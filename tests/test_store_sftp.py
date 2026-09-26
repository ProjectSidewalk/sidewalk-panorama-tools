"""Tests for downloaders/store_sftp.py: pulling already-scraped imagery off the Project Sidewalk store (#30).

Nothing here touches a network. The one subprocess call the module makes is pointed at FAKE_SFTP_SCRIPT, a
Python stand-in for `sftp -b -` that reads the batch on stdin, treats a local directory tree as the "remote",
and models the three batch-mode semantics the design leans on:

  * an unprefixed command that fails ABORTS the session with exit 1 (so the `cd` probe fails a wrong city);
  * a `-`-prefixed command that fails prints to stderr and the session carries on (so one absent pano does not
    cost the other 99);
  * a `get` into a local directory that does not exist fails, as real sftp does (it creates no directories).

Going through the real subprocess.run rather than monkeypatching it means the stdin pipe, the return code and
the stderr path are all exercised: a build that stopped passing `input=` would hand the fake an empty batch
and fail here, where a patched `run` would have recorded whatever it was given.

tests/test_download_runner.py imports write_fake_sftp and the fixtures' helpers from here.
"""

import inspect
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

import numpy as np
import pytest
from PIL import Image

from conftest import posix_only
from downloaders import gsv, store_sftp
from downloaders.common import atomic_output_path, jpeg_dimensions
from downloaders.store_sftp import PullOutcome, StoreSessionError, StoreSettings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_SFTP_SCRIPT = r'''
"""Test-only stand-in for `sftp -b -`. See tests/test_store_sftp.py's module docstring."""
import json
import os
import shlex
import sys

argv = sys.argv[1:]
# Real sftp is byte-transparent and store_sftp sends UTF-8 (a POSIX non-UTF-8 name as its original bytes, via
# surrogateescape); read it back the same way, which is how Python's os functions see such a name.
batch = sys.stdin.buffer.read().decode('utf-8', 'surrogateescape')
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
record = os.environ.get('FAKE_SFTP_RECORD')
if record:
    with open(record, 'a') as f:
        f.write(json.dumps({'argv': argv, 'batch': batch}) + '\n')

if os.environ.get('FAKE_SFTP_FAIL_AUTH'):
    # ssh's own wording embeds the destination, which is the whole reason the module redacts.
    sys.stderr.write('%s: Permission denied (publickey).\r\nConnection closed\r\n' % argv[-1])
    sys.exit(255)

truncate = set(filter(None, os.environ.get('FAKE_SFTP_TRUNCATE', '').split(',')))
cwd = None


def fail(message, ignore):
    sys.stderr.write(message + '\n')
    if not ignore:
        sys.exit(1)


for line in batch.splitlines():
    if not line.strip():
        continue
    print('sftp> ' + line)
    ignore = line.startswith('-')
    parts = shlex.split(line[1:] if ignore else line)
    verb, args = parts[0], parts[1:]
    if any(a.startswith('-') for a in args):
        # Real sftp parses a leading '-' as an option, which is what a relative path to an id beginning
        # with '-' would turn into.
        fail('%s: Invalid flag' % verb, ignore)
        continue
    if verb == 'cd' and len(args) == 1:
        target = args[0] if os.path.isabs(args[0]) else os.path.join(cwd or '.', args[0])
        if not os.path.isdir(target):
            fail("Couldn't canonicalize: No such file or directory", ignore)
            continue
        cwd = target
    elif verb == 'get' and len(args) == 2:
        remote = args[0] if os.path.isabs(args[0]) else os.path.join(cwd or '.', args[0])
        local = args[1]
        if not os.path.isfile(remote):
            fail('File "%s" not found.' % remote, ignore)
            continue
        if not os.path.isdir(os.path.dirname(os.path.abspath(local))):
            fail('Couldn\'t open local file "%s" for writing: No such file or directory' % local, ignore)
            continue
        with open(remote, 'rb') as f:
            data = f.read()
        if os.path.basename(remote) in truncate:
            data = data[:len(data) // 2]
        with open(local, 'wb') as f:
            f.write(data)
    else:
        fail('Invalid command.', ignore)
sys.exit(0)
'''

HOST, USER = 'store.example', 'collab'
CITY = 'seattle-wa'
DASH_ID = '-3KxDRsAbCdEfGhIjKlMnO'        # GSV ids are base64url and can begin with '-' or '_'
UNDERSCORE_ID = '_qVKgG3dGOoClMQI6QgVRg'


def small_jpeg(size=(64, 32)):
    """A real Pillow JPEG, well over twice its header, so a half file keeps a readable SOF and loses its EOI."""
    buf = io.BytesIO()
    Image.effect_noise(size, 60).convert('RGB').save(buf, 'jpeg', quality=95)
    return buf.getvalue()


def with_thumbnail(jpeg):
    """`jpeg` with an EXIF-style APP1 segment inserted after its SOI, holding a whole second JPEG - the
    embedded-thumbnail shape - so the file contains an FF D9 long before its own scan begins."""
    thumb = small_jpeg((16, 8))
    payload = b'Exif\x00\x00' + thumb
    return jpeg[:2] + b'\xff\xe1' + (len(payload) + 2).to_bytes(2, 'big') + payload + jpeg[2:]


def small_npz():
    buf = io.BytesIO()
    np.savez(buf, depth=np.arange(64, dtype=np.float32).reshape(8, 8))
    return buf.getvalue()


def write_fake_sftp(tmp_path, monkeypatch):
    """Point store_sftp at the fake and return the path its session record is appended to."""
    script = tmp_path / 'fake_sftp.py'
    script.write_text(FAKE_SFTP_SCRIPT)
    record = tmp_path / 'sftp_sessions.jsonl'
    monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', [sys.executable, str(script)])
    monkeypatch.setenv('FAKE_SFTP_RECORD', str(record))
    monkeypatch.delenv('FAKE_SFTP_FAIL_AUTH', raising=False)
    monkeypatch.delenv('FAKE_SFTP_TRUNCATE', raising=False)
    return record


def sessions(record):
    """Every session the fake saw, in order: [{'argv': [...], 'batch': '...'}]."""
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


def write_fake_ssh_g(tmp_path, monkeypatch, output):
    """Point store_sftp's `ssh -G` at a stand-in that prints `output`; returns the file its argv (the part
    after the interpreter and script) is written to."""
    script = tmp_path / 'fake_ssh_g.py'
    record = tmp_path / 'ssh_g_argv.txt'
    script.write_text('import sys\nopen(%r, "w").write(" ".join(sys.argv[1:]))\nsys.stdout.write(%r)\n'
                      % (str(record), output))
    monkeypatch.setattr(store_sftp, 'SSH_CONFIG_COMMAND', [sys.executable, str(script), '-G'])
    return record


def make_remote(tmp_path, files, city=CITY):
    """A 'remote' store: <tmp>/remote/panos/<city>/<id[:2]>/<name>. Returns the base (the PS_SFTP_BASE)."""
    base = tmp_path / 'remote' / 'panos'
    (base / city).mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = base / city / name[:2] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return str(base)


def make_settings(base, tmp_path, city=CITY, user=USER, port=None, key='default'):
    if key == 'default':
        key = str(tmp_path / 'keys' / 'id_ed25519')
    return StoreSettings(host=HOST, base=base, user=user, port=port, key=key, remote_city=city)


def batch_tokens(line):
    return shlex.split(line[1:] if line.startswith('-') else line)


class TestSettings:
    def test_host_and_base_are_required_and_named(self):
        with pytest.raises(ValueError) as e:
            store_sftp.resolve_settings(CITY, environ={})
        assert 'PS_SFTP_HOST' in str(e.value) and 'PS_SFTP_BASE' in str(e.value)

    def test_only_the_missing_one_is_named(self):
        with pytest.raises(ValueError) as e:
            store_sftp.resolve_settings(CITY, environ={'PS_SFTP_HOST': HOST})
        assert 'PS_SFTP_BASE' in str(e.value) and 'PS_SFTP_HOST' not in str(e.value)

    def test_the_environment_supplies_every_setting(self):
        s = store_sftp.resolve_settings(CITY, environ={
            'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/panos', 'PS_SFTP_USER': USER, 'PS_SFTP_PORT': '2222',
            'PS_SFTP_KEY': '/k/id'})
        assert s == StoreSettings(HOST, '/panos', USER, '2222', '/k/id', CITY)

    def test_flags_beat_environment(self):
        s = store_sftp.resolve_settings(CITY, host='other.example', base='/b2', user='u2', port='22', key='/k2',
                                        environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/panos',
                                                 'PS_SFTP_USER': USER, 'PS_SFTP_PORT': '2222',
                                                 'PS_SFTP_KEY': '/k/id'})
        assert s == StoreSettings('other.example', '/b2', 'u2', '22', '/k2', CITY)

    def test_optional_user_port_key_stay_none(self):
        s = store_sftp.resolve_settings(CITY, environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/panos'})
        assert (s.user, s.port, s.key) == (None, None, None)

    def test_key_is_user_expanded(self):
        s = store_sftp.resolve_settings(CITY, key='~/id_x', environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/p'})
        assert s.key == os.path.expanduser('~/id_x') and '~' not in s.key

    def test_a_trailing_slash_on_the_base_is_dropped(self):
        s = store_sftp.resolve_settings(CITY, environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/panos/'})
        assert s.base == '/panos'

    @pytest.mark.parametrize('port', ['22a', '-1', ''])
    def test_a_port_must_be_digits(self, port):
        env = {'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/p'}
        if port == '':
            # blank means unset, like every other optional setting
            assert store_sftp.resolve_settings(CITY, port=port, environ=env).port is None
            return
        with pytest.raises(ValueError, match='PS_SFTP_PORT'):
            store_sftp.resolve_settings(CITY, port=port, environ=env)

    def test_the_env_var_names_are_the_log_analyzers(self):
        """The twin in log_analyzer/analyze.py is not this change's to edit, so the copy is held equal here."""
        spec = importlib.util.spec_from_file_location(
            'analyze_for_store_sftp_test', os.path.join(REPO_ROOT, 'log_analyzer', 'analyze.py'))
        analyze = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analyze)
        assert set(re.findall(r'PS_SFTP_[A-Z]+', inspect.getsource(analyze.resolve_sftp))) \
            == set(store_sftp.SFTP_ENV_VARS)

    @pytest.mark.parametrize('bad', ['', 'a/b', '..', '.', 'x"y', 'a b', 'a\n', 'a\\b', 'a*', '-rf'])
    def test_remote_city_id_rejects(self, bad):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            store_sftp.remote_city_id(bad)

    @pytest.mark.parametrize('good', ['seattle-wa', 'cdmx', 'spgg', 'la-piedad', 'st.louis'])
    def test_remote_city_id_accepts(self, good):
        assert store_sftp.remote_city_id(good) == good

    @pytest.mark.parametrize('bad_base', ['/pa"nos', '/panos\n', '/panos\r', '/pan*s', '/pan?s', '/p[a]nos'])
    def test_a_base_the_batch_cannot_quote_is_refused(self, bad_base):
        with pytest.raises(ValueError, match='PS_SFTP_BASE'):
            store_sftp.resolve_settings(CITY, environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': bad_base})

    def test_resolve_settings_validates_the_city_too(self):
        with pytest.raises(ValueError):
            store_sftp.resolve_settings('../etc', environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': '/p'})

    @pytest.mark.parametrize('bad_storage', ['/srv/a"b', '/srv/a\nb', '/srv/a\rb'])
    def test_a_storage_path_the_batch_cannot_quote_is_refused(self, bad_storage):
        with pytest.raises(ValueError):
            store_sftp.build_batch(make_settings('/panos', None, key=None), bad_storage, ['abc'],
                                   store_sftp.IMAGE_SUFFIX)


class TestBatchText:
    def settings(self):
        return StoreSettings(HOST, '/panos', USER, '2222', '/home/collab/.ssh/id_ed25519', CITY)

    def test_first_line_is_an_unprefixed_cd_and_every_get_is_prefixed(self, tmp_path):
        lines = store_sftp.build_batch(self.settings(), str(tmp_path), ['abcdef', 'ghijkl'],
                                       store_sftp.IMAGE_SUFFIX).splitlines()
        assert lines[0] == 'cd "/panos/seattle-wa"'
        assert len(lines) == 3
        assert all(line.startswith('-get ') for line in lines[1:])

    @pytest.mark.parametrize('base,probe', [('/', 'cd "/seattle-wa"'), ('//', 'cd "/seattle-wa"'),
                                            ('/srv/panos/', 'cd "/srv/panos/seattle-wa"'),
                                            ('remote/panos', 'cd "remote/panos/seattle-wa"')])
    def test_the_probe_joins_base_and_city_without_a_doubled_slash(self, tmp_path, base, probe):
        """A chroot base of '/' is exactly the restricted-SFTP case; it used to probe `cd "//seattle-wa"`."""
        settings = store_sftp.resolve_settings(CITY, environ={'PS_SFTP_HOST': HOST, 'PS_SFTP_BASE': base})
        assert store_sftp.build_batch(settings, str(tmp_path), ['abcdef'],
                                      store_sftp.IMAGE_SUFFIX).splitlines()[0] == probe

    def test_the_only_verbs_are_cd_and_get(self, tmp_path):
        """No writes to the store, ever: not put, not mkdir, not rename."""
        text = store_sftp.build_batch(self.settings(), str(tmp_path), ['abcdef', DASH_ID],
                                      store_sftp.DEPTH_SUFFIX)
        assert {batch_tokens(line)[0] for line in text.splitlines()} == {'cd', 'get'}

    def test_remote_paths_are_dot_relative_to_the_probed_dir_and_double_quoted(self, tmp_path):
        """Remote paths are `./<id[:2]>/<id><suffix>`, relative to the directory the `cd` probe just proved
        exists. Not `<base>/<city>/...`: after the `cd`, sftp resolves a RELATIVE base against the new cwd, so
        a relative PS_SFTP_BASE turned every get into `<base>/<city>/<base>/<city>/...` and every pano into
        "absent" with exit 0 (measured on OpenSSH 9.0p1). The `./` also keeps an id beginning with '-' from
        being parsed by `get` as a flag. The local target is absolute for the same reason. Quoted because a
        collaborator's storage path may contain a space."""
        storage = tmp_path / 'my store'
        lines = store_sftp.build_batch(self.settings(), str(storage), [DASH_ID, UNDERSCORE_ID],
                                       store_sftp.IMAGE_SUFFIX).splitlines()
        for line, pano_id in zip(lines[1:], [DASH_ID, UNDERSCORE_ID]):
            tokens = batch_tokens(line)
            assert len(tokens) == 3, line
            assert tokens[1] == './%s/%s.jpg' % (pano_id[:2], pano_id)
            assert tokens[2].startswith(os.path.abspath(str(storage)))
            assert '"%s"' % tokens[1] in line and '"%s"' % tokens[2] in line

    def test_the_local_target_is_what_atomic_output_path_yields(self, tmp_path):
        line = store_sftp.build_batch(self.settings(), str(tmp_path), [DASH_ID],
                                      store_sftp.IMAGE_SUFFIX).splitlines()[1]

        class Sentinel(Exception):
            pass

        final = store_sftp.local_final_path(str(tmp_path), DASH_ID, store_sftp.IMAGE_SUFFIX)
        with pytest.raises(Sentinel):
            with atomic_output_path(final) as part:
                assert part == batch_tokens(line)[2]
                raise Sentinel()
        assert not os.path.exists(final)

    def test_no_connection_detail_is_in_the_batch(self, tmp_path):
        text = store_sftp.build_batch(self.settings(), str(tmp_path), ['abcdef'], store_sftp.IMAGE_SUFFIX)
        for secret in (HOST, USER, '/home/collab/.ssh/id_ed25519', '2222'):
            assert secret not in text

    def test_depth_suffix_builds_the_npz_path(self, tmp_path):
        line = store_sftp.build_batch(self.settings(), str(tmp_path), ['abcdef'],
                                      store_sftp.DEPTH_SUFFIX).splitlines()[1]
        remote, local = batch_tokens(line)[1:]
        assert remote == './ab/abcdef.depth.npz'
        assert local == os.path.join(os.path.abspath(str(tmp_path)), 'ab', 'abcdef.depth.npz.part')

    def test_depth_suffix_matches_gsvs(self):
        assert store_sftp.DEPTH_SUFFIX == gsv.DEPTH_ARTIFACT_SUFFIX

    @pytest.mark.parametrize('pano_id,ok', [
        ('ok-id_1', True), (DASH_ID, True), ('123456789012345', True),
        ('3f9c2a1e-8b7d-4c6e-9a5f-0d1e2f3a4b5c', True),
        ('a"b', False), ('a/b', False), ('a b', False), ('a\tb', False), ('a\nb', False), ('a*b', False),
        ('a?b', False), ('a[b', False), ('a\\b', False), ('', False), ('..', False)])
    def test_an_unsafe_id_is_refused_by_the_predicate(self, pano_id, ok):
        assert store_sftp.is_batch_safe_id(pano_id) is ok

    @pytest.mark.parametrize('pano_id', ['pa\u00f1oAAAAAAAAAAAAAAAAA', 'abc\u0661\u0662', 'abc\uff21'])
    def test_a_non_ascii_id_is_not_batch_safe(self, pano_id):
        """The alphabet is ASCII on purpose: a Unicode \\w would admit these (n-tilde, Arabic-Indic digits,
        a fullwidth letter), and none is an id any of the three sources issues."""
        assert not store_sftp.is_batch_safe_id(pano_id)

    def test_build_batch_refuses_an_unsafe_id(self, tmp_path):
        with pytest.raises(ValueError):
            store_sftp.build_batch(self.settings(), str(tmp_path), ['a"b'], store_sftp.IMAGE_SUFFIX)


class TestArgv:
    def argv(self, **kw):
        fields = dict(host=HOST, base='/panos', user=None, port=None, key=None, remote_city=CITY)
        fields.update(kw)
        return store_sftp.sftp_argv(StoreSettings(**fields))

    def test_batchmode_stdin_batch_and_timeouts_are_always_on(self):
        argv = self.argv()
        options = [argv[i + 1] for i, token in enumerate(argv) if token == '-o']
        assert 'BatchMode=yes' in options
        assert 'StrictHostKeyChecking=accept-new' in options
        assert 'ConnectTimeout=30' in options
        assert any(o.startswith('ServerAliveInterval=') for o in options)
        i = argv.index('-b')
        assert argv[i + 1] == '-'
        assert argv[-1] == HOST

    def test_optional_arguments_are_omitted_when_unset(self):
        argv = self.argv()
        assert '-P' not in argv and '-i' not in argv
        assert argv[-1] == HOST

    def test_optional_arguments_are_included_when_set(self):
        argv = self.argv(user=USER, port='2222', key='/k/id')
        assert argv[argv.index('-P') + 1] == '2222'
        assert argv[argv.index('-i') + 1] == '/k/id'
        assert argv[-1] == '%s@%s' % (USER, HOST)

    def test_the_command_prefix_comes_from_the_seam(self, monkeypatch):
        monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', ['/opt/x', '--flag'])
        assert self.argv()[:2] == ['/opt/x', '--flag']
        assert store_sftp.SFTP_COMMAND == ['/opt/x', '--flag']   # not mutated by the builder


class TestRedaction:
    def settings(self, **kw):
        fields = dict(host=HOST, base='/panos', user=USER, port=None, key='/home/collab/.ssh/id_ed25519',
                      remote_city=CITY)
        fields.update(kw)
        return StoreSettings(**fields)

    def test_host_user_and_key_are_replaced(self):
        text = ('collab@store.example: Permission denied (publickey).\n'
                'Load key "/home/collab/.ssh/id_ed25519": invalid format\n')
        out = store_sftp.redact(text, self.settings())
        for secret in (HOST, USER, '/home/collab/.ssh/id_ed25519'):
            assert secret not in out
        assert '<user>@<host>' in out and '<key>' in out

    def test_redact_skips_empty_values(self):
        """''.replace would insert the placeholder between every character (TokenRedactionFilter's lesson)."""
        out = store_sftp.redact('Connection closed', self.settings(user=None, key=''))
        assert out == 'Connection closed'

    def test_the_summary_is_capped(self):
        text = '\n'.join('x' * 1000 for _ in range(40))
        summary = store_sftp.summarize_stderr(text, self.settings())
        lines = summary.split(' | ')
        assert len(lines) <= store_sftp.STDERR_SUMMARY_LINES
        assert all(len(line) <= store_sftp.STDERR_SUMMARY_CHARS for line in lines)

    def test_the_summary_is_redacted_and_one_line(self):
        summary = store_sftp.summarize_stderr('\r\n\ncollab@store.example: Permission denied\r\n',
                                              self.settings())
        assert '\n' not in summary and '\r' not in summary
        assert HOST not in summary and USER not in summary
        assert 'Permission denied' in summary

    def test_an_empty_stderr_still_says_something(self):
        assert store_sftp.summarize_stderr('', self.settings())

    def test_a_disconnect_after_absent_panos_is_what_the_summary_reports(self):
        """Absent panos routinely precede a disconnect in a 100-get batch. Their per-line `not found` is
        expected noise (the phase counts them from the filesystem), so it is not what a failed session is
        summarised with; the cause, at the end, is."""
        text = ''.join('File "/b/seattle-wa/./Ab/Ab%d.jpg" not found.\n' % i for i in range(6))
        text += 'client_loop: send disconnect: Broken pipe\nConnection closed\n'
        summary = store_sftp.summarize_stderr(text, self.settings())
        assert 'Connection closed' in summary and 'Broken pipe' in summary
        assert 'not found' not in summary

    def test_the_summary_keeps_the_last_lines_not_the_first(self):
        """ssh's host-key banner runs to twenty lines; the verdict is at the bottom."""
        text = '\n'.join(['@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@'] * 20
                         + ['Host key verification failed.', 'Connection closed'])
        summary = store_sftp.summarize_stderr(text, self.settings())
        assert summary.endswith('Host key verification failed. | Connection closed')

    def test_only_not_found_lines_are_still_reported(self):
        """If every line is a `not found`, that is still better than '(no error output)'."""
        summary = store_sftp.summarize_stderr('File "/x/./Ab/Ab1.jpg" not found.\n', self.settings())
        assert 'not found' in summary

    # --- What ssh prints that the configured strings do not cover (#155 review item 2) ----------------
    # Each case isolates ONE mechanism: its text carries nothing any other rule would catch.

    def test_the_configured_port_is_redacted_wherever_it_appears(self):
        """Only settings.port covers this: no `port N`, no `]:N` for the contextual rules to see."""
        out = store_sftp.redact("ssh: Bad port '2222' for store.example", self.settings(port='2222'))
        assert out == "ssh: Bad port '<port>' for <host>"

    def test_the_configured_port_is_not_carved_out_of_ids_and_paths(self):
        """The configured port is replaced as a whole word, like the resolved one: as a substring,
        PS_SFTP_PORT=22 turned ab22xy into ab<port>xy and D:\\2022 into D:\\20<port> (#155 final review)."""
        out = store_sftp.redact('Couldn\'t write to local "D:\\data\\2022\\ab\\ab22xy.jpg.part"; port 22',
                                self.settings(port='22'))
        assert 'ab22xy' in out and '2022' in out and 'port <port>' in out

    def test_the_known_hosts_port_form_is_redacted_even_when_not_configured(self):
        out = store_sftp.redact("Warning: Permanently added '[store.example]:2200' (ED25519)",
                                self.settings(port=None))
        assert out == "Warning: Permanently added '[<host>]:<port>' (ED25519)"

    def test_a_port_named_as_such_is_redacted_even_when_not_configured(self):
        out = store_sftp.redact('ssh: connect to host <host> port 2200: Connection refused',
                                self.settings(port=None))
        assert '2200' not in out and 'port <port>' in out

    @pytest.mark.parametrize('ip', ['128.208.1.2', '2001:db8::1', 'fe80::1ff:fe23:4567:890a', '::1',
                                    '::ffff:10.0.0.7'])
    def test_an_ip_literal_is_redacted(self, ip):
        """An ssh-config alias resolves to an address nothing in settings names."""
        assert store_sftp.redact('Connection closed by %s port 22' % ip, self.settings()) \
            == 'Connection closed by <ip> port <port>'
        assert store_sftp.redact('Connection to %s closed.' % ip, self.settings()) == 'Connection to <ip> closed.'

    def test_any_user_at_prefix_is_redacted(self):
        """With the user unset, ~/.ssh/config's User is what ssh prints; the configured strings cannot know it."""
        out = store_sftp.redact('realuser@psstore: Permission denied (publickey).', self.settings(user=None))
        assert 'realuser' not in out and out.startswith('<user>@')

    def test_ordinary_messages_survive_the_patterns(self):
        for text in ('Connection closed', 'stat remote: No such file or directory', 'sftp exited 255',
                     'Permission denied (publickey).', 'client_loop: send disconnect: Broken pipe'):
            assert store_sftp.redact(text, self.settings()) == text

    def test_the_values_ssh_config_resolves_are_redacted(self, tmp_path, monkeypatch):
        """An alias's HostName, User and Port as `ssh -G <host>` resolves them: none is in settings, and
        none of them, in these positions, is something the patterns alone would catch."""
        record = write_fake_ssh_g(tmp_path, monkeypatch, 'hostname real-store.cs.example\nuser realuser\n'
                                                         'port 2201\nidentityfile ~/.ssh/id_x\n')
        settings = self.settings(host='psstore', user=None, key=None)
        summary = store_sftp.summarize_stderr(
            'ssh: Could not resolve hostname real-store.cs.example: Name or service not known\n'
            'Load key "/home/realuser/.ssh/id_x": invalid format\n'
            "Warning: Permanently added '[psstore]:2201' (ED25519)\n", settings)
        for secret in ('real-store.cs.example', 'realuser', '2201', 'psstore'):
            assert secret not in summary, summary
        assert record.read_text().split() == ['-G', 'psstore']

    def test_ssh_config_is_asked_with_the_configured_user_and_port(self, tmp_path, monkeypatch):
        record = write_fake_ssh_g(tmp_path, monkeypatch, 'hostname h\n')
        store_sftp.ssh_config_values(self.settings(port='2222'))
        assert record.read_text().split() == ['-G', '-p', '2222', '-l', USER, HOST]

    def test_a_generic_resolved_value_is_replaced_only_as_a_whole_word(self, tmp_path, monkeypatch):
        """ssh -G always prints a user and a port - on a plain host, the LOCAL login and 22 - so a resolved
        value is replaced only where it stands alone, never inside an id or another word."""
        write_fake_ssh_g(tmp_path, monkeypatch, 'hostname store.example\nuser ab\nport 22\n')
        summary = store_sftp.summarize_stderr('sftp exited 1: 22abc ab-x cab 22 ab', self.settings())
        assert summary == 'sftp exited 1: 22abc ab-x cab <port> <user>'

    @pytest.mark.parametrize('behaviour', ['missing', 'fails', 'garbage', 'hangs'])
    def test_ssh_config_trouble_never_stops_the_redaction(self, tmp_path, monkeypatch, behaviour):
        if behaviour == 'missing':
            monkeypatch.setattr(store_sftp, 'SSH_CONFIG_COMMAND', [str(tmp_path / 'no-such-ssh'), '-G'])
        else:
            script = tmp_path / 'ssh_g.py'
            script.write_text({
                # A nonzero exit is not trusted even when something was printed first.
                'fails': 'import sys\nsys.stdout.write("hostname half-read\\n")\nsys.exit(255)\n',
                'garbage': 'import sys\nsys.stdout.buffer.write(bytes([255, 254]) + b" hostname\\n\\n x")\n',
                'hangs': 'import time\ntime.sleep(20)\nprint("hostname too-late")\n'}[behaviour])
            monkeypatch.setattr(store_sftp, 'SSH_CONFIG_COMMAND', [sys.executable, str(script), '-G'])
            monkeypatch.setattr(store_sftp, 'SSH_CONFIG_TIMEOUT_SECONDS', 0.5)
        started = time.monotonic()
        assert not any(store_sftp.ssh_config_values(self.settings()).values())
        assert time.monotonic() - started < 10, 'the lookup must be bounded by SSH_CONFIG_TIMEOUT_SECONDS'
        summary = store_sftp.summarize_stderr('collab@store.example: Permission denied', self.settings())
        assert summary == '<user>@<host>: Permission denied'


class TestVerifiers:
    def test_a_complete_jpeg_passes(self, tmp_path):
        p = tmp_path / 'a.jpg'
        p.write_bytes(small_jpeg())
        assert store_sftp.is_complete_jpeg(str(p))

    def test_a_half_jpeg_keeps_its_header_and_still_fails(self, tmp_path):
        """The reason the EOI check exists: the header alone reports full dimensions on a half file."""
        data = small_jpeg()
        p = tmp_path / 'a.jpg'
        p.write_bytes(data[:len(data) // 2])
        assert jpeg_dimensions(str(p)) == (64, 32)
        assert not store_sftp.is_complete_jpeg(str(p))

    def test_an_eoi_without_a_header_fails(self, tmp_path):
        p = tmp_path / 'a.jpg'
        p.write_bytes(b'<html>not found</html>\xff\xd9')
        assert not store_sftp.is_complete_jpeg(str(p))

    def test_a_missing_or_empty_file_fails(self, tmp_path):
        assert not store_sftp.is_complete_jpeg(str(tmp_path / 'nope.jpg'))
        (tmp_path / 'empty.jpg').write_bytes(b'')
        assert not store_sftp.is_complete_jpeg(str(tmp_path / 'empty.jpg'))
        (tmp_path / 'one.jpg').write_bytes(b'\xd9')
        assert not store_sftp.is_complete_jpeg(str(tmp_path / 'one.jpg'))

    # --- Bytes after the end-of-image marker (#155 review item 23) --------------------------------------
    # Mapillary and Panoramax originals are stored verbatim, and a camera JPEG may carry trailing bytes (a
    # vendor trailer, padding). The strict "ends with FF D9" rule refused, deleted and re-pulled such a file
    # every night. The rule now also accepts an EOI in the file's last TRAILING_BYTES_WINDOW bytes, provided
    # it lies after the start of the main image's first scan - which no thumbnail's EOI can.

    def test_a_jpeg_with_trailing_bytes_after_its_eoi_passes(self, tmp_path):
        p = tmp_path / 'a.jpg'
        p.write_bytes(small_jpeg() + b'\x00' * 1000 + b'VENDOR-TRAILER')
        assert store_sftp.is_complete_jpeg(str(p))

    def test_trailing_bytes_beyond_the_window_are_refused(self, tmp_path):
        p = tmp_path / 'a.jpg'
        p.write_bytes(small_jpeg() + b'\x00' * (store_sftp.TRAILING_BYTES_WINDOW + 1))
        assert not store_sftp.is_complete_jpeg(str(p))

    def test_a_trailing_window_that_ends_exactly_at_the_limit_passes(self, tmp_path):
        p = tmp_path / 'a.jpg'
        p.write_bytes(small_jpeg() + b'\x00' * (store_sftp.TRAILING_BYTES_WINDOW - 2))
        assert store_sftp.is_complete_jpeg(str(p))

    def test_a_truncation_cannot_pass_on_an_embedded_thumbnails_eoi(self, tmp_path):
        """An EXIF thumbnail is a whole JPEG, FF D9 included, inside an APP1 segment before the main image.
        A transfer cut short a little way into the main scan has that EOI well inside the window."""
        data = with_thumbnail(small_jpeg())
        sos = data.index(b'\xff\xda', data.index(b'\xff\xd9') + 2)
        p = tmp_path / 'a.jpg'
        p.write_bytes(data)
        assert store_sftp.is_complete_jpeg(str(p)), 'the whole file, thumbnail and all, is complete'
        for cut in (sos + 40, sos + 2, sos - 10):
            p.write_bytes(data[:cut])
            assert not store_sftp.is_complete_jpeg(str(p)), cut

    def test_a_half_jpeg_with_a_thumbnail_still_fails(self, tmp_path):
        data = with_thumbnail(small_jpeg())
        p = tmp_path / 'a.jpg'
        p.write_bytes(data[:len(data) // 2])
        assert jpeg_dimensions(str(p)) is not None
        assert not store_sftp.is_complete_jpeg(str(p))

    def test_a_jpeg_with_no_scan_fails(self, tmp_path):
        """A header with an EOI but no SOS: nothing to say the image data is there at all."""
        data = small_jpeg()
        p = tmp_path / 'a.jpg'
        p.write_bytes(data[:data.index(b'\xff\xda')] + b'\xff\xd9' + b'\x00' * 10)
        assert not store_sftp.is_complete_jpeg(str(p))

    @pytest.mark.parametrize('data,offset', [
        (b'NOTAJPEG', None),                                          # no SOI
        (b'\xff\xd8\x00\xff\xda', None),                              # a non-marker byte between segments
        (b'\xff\xd8\xff\xff', None),                                  # fill bytes, then the end of the file
        (b'\xff\xd8\xff\xd9\xff\xda\x00\x02', None),                  # EOI before any scan
        (b'\xff\xd8\xff\xe0\x00\x01\xff\xda\x00\x02', None),          # a segment length below its own 2 bytes
        (b'\xff\xd8\xff\xe0\x00', None),                              # the header ends inside a length
        (b'\xff\xd8\xff\xd0\xff\xff\xda\x00\x04ab' + b'scan', 11),    # RSTn standalone, fill, SOS
    ])
    def test_the_scan_start_walk(self, tmp_path, data, offset):
        p = tmp_path / 'h.bin'
        p.write_bytes(data)
        with open(p, 'rb') as f:
            assert store_sftp._first_scan_offset(f) == offset

    def test_the_npz_verifier(self, tmp_path):
        data = small_npz()
        whole, half, jpg = tmp_path / 'a.npz', tmp_path / 'b.npz', tmp_path / 'c.jpg'
        whole.write_bytes(data)
        half.write_bytes(data[:len(data) // 2])
        jpg.write_bytes(small_jpeg())
        assert store_sftp.is_complete_npz(str(whole))
        assert not store_sftp.is_complete_npz(str(half))
        assert not store_sftp.is_complete_npz(str(jpg))
        assert not store_sftp.is_complete_jpeg(str(whole))
        assert not store_sftp.is_complete_npz(str(tmp_path / 'missing.npz'))


class TestPullBatch:
    def pull(self, settings, storage, ids, suffix=store_sftp.IMAGE_SUFFIX, verifier=None):
        verifier = verifier or (store_sftp.is_complete_jpeg if suffix == store_sftp.IMAGE_SUFFIX
                                else store_sftp.is_complete_npz)
        return store_sftp.pull_batch(settings, str(storage), ids, suffix, verifier)

    def test_a_present_pano_is_pulled_verified_and_placed(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        data = small_jpeg()
        base = make_remote(tmp_path, {'abcdef.jpg': data})
        storage = tmp_path / 'local'
        outcomes = self.pull(make_settings(base, tmp_path), storage, ['abcdef'])
        assert outcomes == {'abcdef': PullOutcome.pulled}
        final = storage / 'ab' / 'abcdef.jpg'
        assert final.read_bytes() == data
        assert not (storage / 'ab' / 'abcdef.jpg.part').exists()

    @posix_only
    def test_modes_match_the_downloaders(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'
        self.pull(make_settings(base, tmp_path), storage, ['abcdef'])
        assert stat.S_IMODE(os.stat(storage / 'ab' / 'abcdef.jpg').st_mode) == 0o664
        assert stat.S_IMODE(os.stat(storage / 'ab').st_mode) == 0o2775

    def test_an_absent_pano_in_the_middle_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'aaaaaa.jpg': small_jpeg(), 'cccccc.jpg': small_jpeg()})
        outcomes = self.pull(make_settings(base, tmp_path), tmp_path / 'local', ['aaaaaa', 'bbbbbb', 'cccccc'])
        assert outcomes == {'aaaaaa': PullOutcome.pulled, 'bbbbbb': PullOutcome.absent,
                            'cccccc': PullOutcome.pulled}

    def test_a_relative_base_is_pulled_not_read_as_absent(self, tmp_path, monkeypatch):
        """PS_SFTP_BASE relative to the login directory (the fake's cwd is ours). Before the `./` gets, the
        probe passed and every get looked under <base>/<city>/<base>/<city>: all absent, exit 0."""
        write_fake_sftp(tmp_path, monkeypatch)
        make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        monkeypatch.chdir(tmp_path)
        storage = tmp_path / 'local'
        outcomes = self.pull(make_settings('remote/panos', tmp_path), storage, ['abcdef', DASH_ID])
        assert outcomes == {'abcdef': PullOutcome.pulled, DASH_ID: PullOutcome.absent}
        assert (storage / 'ab' / 'abcdef.jpg').exists()

    def test_an_id_beginning_with_a_dash_is_pulled(self, tmp_path, monkeypatch):
        """The fake rejects a get argument that starts with '-', as real sftp's option parser would."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {DASH_ID + '.jpg': small_jpeg(), UNDERSCORE_ID + '.jpg': small_jpeg()})
        outcomes = self.pull(make_settings(base, tmp_path), tmp_path / 'local', [DASH_ID, UNDERSCORE_ID])
        assert set(outcomes.values()) == {PullOutcome.pulled}

    def test_a_truncated_transfer_is_removed_and_reported_not_placed(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        monkeypatch.setenv('FAKE_SFTP_TRUNCATE', 'abcdef.jpg')
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'
        outcomes = self.pull(make_settings(base, tmp_path), storage, ['abcdef'])
        assert outcomes == {'abcdef': PullOutcome.truncated}
        assert not (storage / 'ab' / 'abcdef.jpg').exists()
        assert not (storage / 'ab' / 'abcdef.jpg.part').exists()

    def test_a_body_that_is_not_a_jpeg_is_refused(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': b'<html>502 Bad Gateway</html>\xff\xd9'})
        storage = tmp_path / 'local'
        assert self.pull(make_settings(base, tmp_path), storage, ['abcdef']) == {'abcdef': PullOutcome.truncated}
        assert not (storage / 'ab' / 'abcdef.jpg').exists()

    def test_a_stale_part_from_a_killed_run_is_not_mistaken_for_tonights_transfer(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {})
        storage = tmp_path / 'local'
        (storage / 'ab').mkdir(parents=True)
        (storage / 'ab' / 'abcdef.jpg.part').write_bytes(small_jpeg())   # complete-looking debris
        outcomes = self.pull(make_settings(base, tmp_path), storage, ['abcdef'])
        assert outcomes == {'abcdef': PullOutcome.absent}
        assert not (storage / 'ab' / 'abcdef.jpg').exists()
        assert not (storage / 'ab' / 'abcdef.jpg.part').exists()

    def test_shard_dirs_exist_before_the_get(self, tmp_path, monkeypatch):
        """Real sftp creates no local directories; the fake fails a get into a missing one the same way."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'zzzzzz.jpg': small_jpeg()})
        storage = tmp_path / 'local'
        assert not storage.exists()
        assert self.pull(make_settings(base, tmp_path), storage, ['zzzzzz']) == {'zzzzzz': PullOutcome.pulled}

    def test_a_shard_dir_chmod_lost_to_another_user_is_not_fatal(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'zzzzzz.jpg': small_jpeg()})

        def not_yours(path, mode):
            raise PermissionError(1, 'Operation not permitted')

        monkeypatch.setattr(store_sftp.os, 'chmod', not_yours)
        storage = tmp_path / 'local'
        # the placed file's own chmod goes through common's os, which is the same module object - so the
        # pano is unplaced, but the shard dir survived its chmod failure and the session ran
        outcome = self.pull(make_settings(base, tmp_path), storage, ['zzzzzz'])
        assert (storage / 'zz').is_dir()
        assert outcome == {'zzzzzz': PullOutcome.unplaced}

    def test_no_session_is_opened_for_an_empty_batch(self, tmp_path, monkeypatch):
        record = write_fake_sftp(tmp_path, monkeypatch)
        assert self.pull(make_settings('/panos', tmp_path), tmp_path / 'local', []) == {}
        assert not record.exists()

    def test_a_nonzero_exit_is_a_session_error_naming_no_secret(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        monkeypatch.setenv('FAKE_SFTP_FAIL_AUTH', '1')
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        settings = make_settings(base, tmp_path)
        with pytest.raises(StoreSessionError) as e:
            self.pull(settings, tmp_path / 'local', ['abcdef'])
        message = str(e.value)
        assert '<host>' in message and '<user>' in message and 'Permission denied' in message
        for secret in (HOST, USER, settings.key):
            assert secret not in message
        assert '255' in message

    def test_a_wrong_city_dir_fails_the_session_on_the_probe(self, tmp_path, monkeypatch):
        record = write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'
        # The exit status, not the wording: the fake says "Couldn't canonicalize", real OpenSSH says
        # "stat remote: No such file or directory", and the probe's contract is only that the session fails.
        with pytest.raises(StoreSessionError, match='exited 1'):
            self.pull(make_settings(base, tmp_path, city='seattle'), storage, ['abcdef', 'ghijkl'])
        assert len(sessions(record)) == 1
        # nothing half-done is left behind for the next run to reason about
        assert not list(storage.rglob('*.part'))

    def test_a_session_error_removes_this_batchs_part_files(self, tmp_path, monkeypatch):
        """A session that dies mid-batch can leave complete-looking .part files; none survive the raise."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'

        def dies_after_the_transfer(settings, batch_text):
            (storage / 'ab' / 'abcdef.jpg.part').write_bytes(small_jpeg())
            import subprocess
            return subprocess.CompletedProcess([], 255, '', 'Connection reset\n')

        monkeypatch.setattr(store_sftp, 'run_sftp_batch', dies_after_the_transfer)
        with pytest.raises(StoreSessionError):
            self.pull(make_settings(base, tmp_path), storage, ['abcdef'])
        assert not (storage / 'ab' / 'abcdef.jpg.part').exists()
        assert not (storage / 'ab' / 'abcdef.jpg').exists()

    def test_a_session_killed_by_a_signal_is_a_session_error(self, tmp_path, monkeypatch):
        """subprocess reports a signal death (the OOM killer, an operator's kill) as a NEGATIVE returncode;
        it is still a failed session, and its .part files are still removed."""
        storage = tmp_path / 'local'

        def killed(settings, batch_text):
            (storage / 'ab' / 'abcdef.jpg.part').write_bytes(small_jpeg())
            return subprocess.CompletedProcess([], -9, '', '')

        monkeypatch.setattr(store_sftp, 'run_sftp_batch', killed)
        with pytest.raises(StoreSessionError, match='-9'):
            self.pull(make_settings('/panos', tmp_path), storage, ['abcdef'])
        assert not list(storage.rglob('abcdef*'))

    def test_a_stop_mid_session_removes_this_batchs_part_files_and_propagates(self, tmp_path, monkeypatch):
        """SIGTERM arrives as SystemExit while subprocess.run waits; run() kills the child and re-raises. The
        killed session's .part files must not be left for a later run to reason about, and the stop must
        reach the runner as itself, not as a StoreSessionError."""
        storage = tmp_path / 'local'

        def stopped(settings, batch_text):
            (storage / 'ab' / 'abcdef.jpg.part').write_bytes(small_jpeg()[:100])
            raise SystemExit(143)

        monkeypatch.setattr(store_sftp, 'run_sftp_batch', stopped)
        with pytest.raises(SystemExit):
            self.pull(make_settings('/panos', tmp_path), storage, ['abcdef', 'ghijkl'])
        assert not list(storage.rglob('*.part'))

    def test_a_non_ascii_storage_path_is_pulled(self, tmp_path, monkeypatch):
        """The batch goes to sftp as UTF-8 whatever the locale: under text=True a cp1252 box raised
        UnicodeEncodeError out of pull_batch - not the OSError it catches, so past the cleanup."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'store_\u03a9_Jos\u00e9'
        assert self.pull(make_settings(base, tmp_path), storage, ['abcdef']) == {'abcdef': PullOutcome.pulled}
        assert (storage / 'ab' / 'abcdef.jpg').exists()

    def test_a_surrogate_escaped_path_reaches_sftp_as_its_original_bytes(self, tmp_path, monkeypatch):
        """On POSIX a directory name that is not valid UTF-8 (byte 0xFF, say) reaches Python as a str with a
        lone surrogate ('\\udcff'), and os functions turn it back into the original byte. A strict
        .encode('utf-8') raised UnicodeEncodeError - a traceback after scrape.log exists - where
        surrogateescape hands sftp, which is byte-transparent on POSIX, the name the filesystem holds.
        Platform-independent: the encoding is checked at the subprocess boundary."""
        seen = {}

        def fake_run(argv, input, capture_output):
            seen['input'] = input
            return subprocess.CompletedProcess(argv, 0, b'', b'')

        monkeypatch.setattr(store_sftp.subprocess, 'run', fake_run)
        store_sftp.run_sftp_batch(make_settings('/panos', tmp_path), '-get "./ab/abcdef.jpg" "/d/\udcff/x"\n')
        assert b'"/d/\xff/x"' in seen['input']

    @pytest.mark.skipif(sys.platform == 'win32',
                        reason='Windows paths are UTF-16 and bytes paths are strict UTF-8: no surrogate-escaped '
                               'name to make')
    def test_a_non_utf8_storage_dir_is_pulled_on_posix(self, tmp_path, monkeypatch):
        """End to end on a real non-UTF-8 directory name. macOS (APFS) refuses to create one; skip there."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        raw = os.path.join(os.fsencode(str(tmp_path)), b'store_\xff')
        try:
            os.mkdir(raw)
        except OSError:
            pytest.skip('this filesystem refuses a non-UTF-8 name')
        storage = os.fsdecode(raw)
        assert '\udcff' in storage
        assert self.pull(make_settings(base, tmp_path), storage, ['abcdef']) == {'abcdef': PullOutcome.pulled}
        assert os.path.exists(os.path.join(raw, b'ab', b'abcdef.jpg'))

    def test_undecodable_error_output_is_still_a_session_error(self, tmp_path, monkeypatch):
        """Bytes no codec accepts (0x81 is undefined in cp1252 and invalid UTF-8) must reach the summary as
        replacement characters, not raise UnicodeDecodeError past the redaction."""
        script = tmp_path / 'bad_bytes_sftp.py'
        script.write_text('import sys\nsys.stdin.buffer.read()\n'
                          'sys.stderr.buffer.write(b"\\x81\\x8d collab@store.example: Permission denied\\n")\n'
                          'sys.exit(255)\n')
        monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', [sys.executable, str(script)])
        with pytest.raises(StoreSessionError) as e:
            self.pull(make_settings('/panos', tmp_path), tmp_path / 'local', ['abcdef'])
        assert '<user>@<host>: Permission denied' in str(e.value)

    def test_sftp_that_cannot_start_is_a_session_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', [str(tmp_path / 'no-such-sftp-binary')])
        with pytest.raises(StoreSessionError, match='could not start'):
            self.pull(make_settings('/panos', tmp_path), tmp_path / 'local', ['abcdef'])

    def test_the_batch_reaches_sftp_on_stdin_and_the_argv_is_the_builders(self, tmp_path, monkeypatch):
        record = write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        settings = make_settings(base, tmp_path, port='2222')
        storage = tmp_path / 'local'
        self.pull(settings, storage, ['abcdef', 'ghijkl'])
        (session,) = sessions(record)
        assert session['batch'] == store_sftp.build_batch(settings, str(storage), ['abcdef', 'ghijkl'],
                                                          store_sftp.IMAGE_SUFFIX)
        assert session['argv'] == store_sftp.sftp_argv(settings)[len(store_sftp.SFTP_COMMAND):]

    def test_a_placement_failure_is_per_pano_and_leaves_nothing(self, tmp_path, monkeypatch):
        import downloaders.common
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'

        def full_disk(src, dst):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(downloaders.common.os, 'replace', full_disk)
        assert self.pull(make_settings(base, tmp_path), storage, ['abcdef']) == {'abcdef': PullOutcome.unplaced}
        assert not list(storage.rglob('abcdef*'))

    def test_a_part_that_cannot_be_read_is_a_verification_failure_not_unplaced(self, tmp_path, monkeypatch):
        """`unplaced` means the rename failed (local storage trouble). An OSError while READING the .part to
        verify it is a verification failure: it is reported `truncated`, and the .part is removed."""
        write_fake_sftp(tmp_path, monkeypatch)
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'

        def unreadable(path):
            raise PermissionError(13, 'Permission denied')

        assert self.pull(make_settings(base, tmp_path), storage, ['abcdef'], verifier=unreadable) \
            == {'abcdef': PullOutcome.truncated}
        assert not list(storage.rglob('abcdef*'))

    def test_a_depth_artifact_is_pulled_and_a_half_one_is_not(self, tmp_path, monkeypatch):
        write_fake_sftp(tmp_path, monkeypatch)
        monkeypatch.setenv('FAKE_SFTP_TRUNCATE', 'bbbbbb.depth.npz')
        base = make_remote(tmp_path, {'aaaaaa.depth.npz': small_npz(), 'bbbbbb.depth.npz': small_npz()})
        storage = tmp_path / 'local'
        outcomes = self.pull(make_settings(base, tmp_path), storage, ['aaaaaa', 'bbbbbb'],
                             suffix=store_sftp.DEPTH_SUFFIX)
        assert outcomes == {'aaaaaa': PullOutcome.pulled, 'bbbbbb': PullOutcome.truncated}
        assert (storage / 'aa' / 'aaaaaa.depth.npz').exists()
        assert not (storage / 'bb' / 'bbbbbb.depth.npz').exists()


# --- The real sftp client, no network (#155 review item 13) ---------------------------------------------------
#
# The fake above models the batch semantics the design leans on; this runs them through OpenSSH's own batch
# parser. `sftp -D <sftp-server>` starts the server as a local subprocess instead of connecting, so the batch,
# the `-`/unprefixed semantics, the quoting and the exit status are all real and nothing leaves the machine.
# POSIX-only (on Windows, Git's msys sftp translates the local paths) and skipped wherever the server binary
# is not installed - on Ubuntu it ships in openssh-sftp-server.

SFTP_SERVER_PATHS = ('/usr/lib/openssh/sftp-server', '/usr/lib/ssh/sftp-server', '/usr/libexec/openssh/sftp-server',
                     '/usr/libexec/sftp-server', '/usr/lib/sftp-server')


def real_sftp_command():
    """['sftp', '-D', <sftp-server>] when both are installed on a POSIX host, else None."""
    if os.name != 'posix':
        return None
    client = shutil.which('sftp')
    server = next((path for path in SFTP_SERVER_PATHS if os.access(path, os.X_OK)), None)
    return [client, '-D', server] if client and server else None


real_sftp = pytest.mark.skipif(real_sftp_command() is None,
                               reason='needs a POSIX host with the sftp client and sftp-server installed')


@real_sftp
class TestAgainstRealOpenSSH:
    def test_the_batch_is_pulled_by_the_real_client(self, tmp_path, monkeypatch):
        """A relative base (item 1), an id beginning with '-', an absent pano between two present ones, and
        a storage path with a space - through OpenSSH's batch parser and exit status."""
        monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', real_sftp_command())
        data = small_jpeg()
        make_remote(tmp_path, {'abcdef.jpg': data, DASH_ID + '.jpg': data})
        monkeypatch.chdir(tmp_path)
        storage = tmp_path / 'local store'
        outcomes = store_sftp.pull_batch(make_settings('remote/panos', tmp_path, key=None), str(storage),
                                         ['abcdef', 'absentPanoAAAAAAAAAAAA', DASH_ID], store_sftp.IMAGE_SUFFIX,
                                         store_sftp.is_complete_jpeg)
        assert outcomes == {'abcdef': PullOutcome.pulled, 'absentPanoAAAAAAAAAAAA': PullOutcome.absent,
                            DASH_ID: PullOutcome.pulled}
        assert (storage / 'ab' / 'abcdef.jpg').read_bytes() == data
        assert (storage / DASH_ID[:2] / (DASH_ID + '.jpg')).read_bytes() == data
        assert not list(storage.rglob('*.part'))

    def test_a_wrong_city_fails_the_session_on_the_real_probe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_sftp, 'SFTP_COMMAND', real_sftp_command())
        base = make_remote(tmp_path, {'abcdef.jpg': small_jpeg()})
        storage = tmp_path / 'local'
        with pytest.raises(StoreSessionError, match='exited 1'):
            store_sftp.pull_batch(make_settings(base, tmp_path, city='seattle', key=None), str(storage),
                                  ['abcdef'], store_sftp.IMAGE_SUFFIX, store_sftp.is_complete_jpeg)
        assert not list(storage.rglob('abcdef*'))


def test_the_module_imports_nothing_that_contacts_a_provider():
    """Store mode never contacts Google, Mapillary or Panoramax; the transport module cannot even reach them."""
    source = inspect.getsource(store_sftp)
    assert not re.search(r'^\s*(from|import)\s+.*\b(gsv|mapillary|panoramax|requests|aiohttp|streetlevel)\b',
                         source, re.M)
