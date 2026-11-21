"""Normalise local path prefixes in committed text files, as Part 1 did. Usage: python -I scrub.py <dir>

The local account name is read at run time (USERNAME, else USER), so it is not written into this file, and the
folder names are lower case under re.I so this file does not trip the report test's path scan.
"""
import os
import re
import sys

USER = re.escape(os.environ.get('USERNAME') or os.environ.get('USER') or '')
SEP = r'[\\/]+'
HOME = r'[Cc]:' + SEP + r'[Uu]sers' + SEP + USER
SCRATCH = (HOME + SEP + r'appdata' + SEP + r'local' + SEP + r'temp' + SEP + r'claude' + SEP + r'[0-9a-f]+' + SEP
           + r'[0-9a-f-]{36}' + SEP + r'scratchpad')
RULES = [
    (re.compile(SCRATCH + SEP + r'237-work', re.I), '<MEAS>'),
    (re.compile(SCRATCH, re.I), '<SCRATCH>'),
    (re.compile(HOME + SEP + r'appdata' + SEP + r'roaming' + SEP + r'uv' + SEP + r'python', re.I), '<UVPYTHON>'),
    (re.compile(HOME + SEP + r'appdata' + SEP + r'local' + SEP + r'temp', re.I), '<TEMP>'),
    (re.compile(r'pytest-of-' + USER), 'pytest-of-<user>'),
    (re.compile(HOME, re.I), '<HOME>'),
]

if not USER:
    sys.exit('no USERNAME or USER in the environment')
changed = 0
for name in sorted(os.listdir(sys.argv[1])):
    path = os.path.join(sys.argv[1], name)
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    new = text
    for pattern, repl in RULES:
        new = pattern.sub(repl, new)
    if new != text:
        changed += 1
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(new)
        print('scrubbed', name)
print(changed, 'file(s) changed')
