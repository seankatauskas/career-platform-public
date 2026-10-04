"""Single-owner native Codex authentication. Credentials never enter reviewer workers."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import threading
import time

CODEX_VERSION = '0.160.0'


class AuthenticationUnavailable(RuntimeError):
    pass


class NativeAuthOwner:
    """Use one dedicated auth home on one machine; never clone a desktop session.

    Native app-server owns OAuth rotation. flock serializes all access and refresh
    across coordinator processes. Only this trusted class reads the resulting file.
    """

    def __init__(self, auth_home, codex_executable='codex', *, timeout=120):
        self.auth_home = Path(auth_home).absolute()
        self.executable = str(codex_executable)
        self.timeout = timeout

    @contextmanager
    def _locked(self):
        self.auth_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.auth_home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AuthenticationUnavailable('Codex authentication directory must be private and owned by this process')
        fd = os.open(self.auth_home / '.review-auth.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthenticationUnavailable('Codex authentication owner is busy') from None
                    time.sleep(.05)
            yield
        finally:
            os.close(fd)

    def _environment(self):
        # No AWS, SSH, dashboard, MCP, API-key, or caller Codex configuration.
        return {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                'HOME': str(self.auth_home), 'CODEX_HOME': str(self.auth_home),
                'LANG': 'C.UTF-8'}

    @contextmanager
    def _server(self):
        version = subprocess.run([self.executable, '--version'], env=self._environment(),
                                 capture_output=True, text=True, timeout=10)
        if version.returncode or version.stdout.strip() != 'codex-cli ' + CODEX_VERSION:
            raise AuthenticationUnavailable('Pinned Codex authentication binary is unavailable')
        command = [self.executable, 'app-server', '--listen', 'stdio://',
                   '-c', 'features.apps=false', '-c', 'features.hooks=false',
                   '-c', 'features.memories=false', '-c', 'analytics.enabled=false',
                   '-c', 'features.plugins=false', '-c', 'features.remote_plugin=false',
                   '-c', 'cli_auth_credentials_store="file"']
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=self._environment(),
                                   cwd=self.auth_home, text=True, start_new_session=True)
        events = queue.Queue(maxsize=256)

        def collect():
            try:
                for line in process.stdout:
                    if len(line) > 65536:
                        break
                    try:
                        events.put(json.loads(line), timeout=1)
                    except (ValueError, queue.Full):
                        break
            finally:
                try:
                    events.put(None, timeout=1)
                except queue.Full:
                    pass

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()

        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()

        def receive(predicate, timeout=None):
            deadline = time.monotonic() + (timeout or self.timeout)
            while time.monotonic() < deadline:
                try:
                    value = events.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if value is None:
                    break
                if predicate(value):
                    if value.get('error'):
                        raise AuthenticationUnavailable('Native Codex authentication failed; sign in again if needed')
                    return value
            raise AuthenticationUnavailable('Native Codex authentication did not complete')

        try:
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
                'name': 'career_review_auth', 'version': '1'}}})
            receive(lambda v: v.get('id') == 1)
            send({'method': 'initialized'})
            yield send, receive
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                stream.close()
            thread.join(timeout=1)

    def _refresh(self):
        with self._server() as (send, receive):
            send({'id': 2, 'method': 'account/read', 'params': {'refreshToken': True}})
            result = receive(lambda v: v.get('id') == 2).get('result', {})
            if (result.get('account') or {}).get('type') != 'chatgpt':
                raise AuthenticationUnavailable('A dedicated ChatGPT sign-in is required; API billing fallback is disabled')

    def login_device(self, announce=print):
        with self._locked(), self._server() as (send, receive):
            send({'id': 2, 'method': 'account/login/start', 'params': {'type': 'chatgptDeviceCode'}})
            result = receive(lambda v: v.get('id') == 2).get('result', {})
            if result.get('type') != 'chatgptDeviceCode' or result.get('verificationUrl') != 'https://auth.openai.com/codex/device':
                raise AuthenticationUnavailable('Unexpected native device sign-in response')
            announce(json.dumps({'verification_url': result['verificationUrl'], 'user_code': result['userCode']}))
            done = receive(lambda v: v.get('method') == 'account/login/completed', timeout=600)
            if not done.get('params', {}).get('success'):
                raise AuthenticationUnavailable('Dedicated Codex sign-in did not succeed')
        return {'authenticated': True, 'auth_mode': 'chatgpt'}

    def _read(self):
        fd = os.open(self.auth_home / 'auth.json', os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
                raise AuthenticationUnavailable('Codex credential file is not private')
            with os.fdopen(fd, 'r') as stream:
                fd = -1
                value = json.load(stream)
        except (ValueError, UnicodeError):
            raise AuthenticationUnavailable('Codex credential file is invalid') from None
        finally:
            if fd >= 0:
                os.close(fd)
        tokens = value.get('tokens') or {}
        if value.get('auth_mode') != 'chatgpt' or value.get('OPENAI_API_KEY') or not all(
                isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'refresh_token', 'account_id')):
            raise AuthenticationUnavailable('Dedicated native ChatGPT credentials are required')
        return tokens

    def _request_headers(self, *, refresh=False):
        """Trusted gateway only: never serialize this return value or log headers."""
        with self._locked():
            try:
                tokens = self._read()
                part = tokens['access_token'].split('.')[1]
                expires = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('exp', 0)
                if not isinstance(expires, (int, float)) or expires < time.time() + 300:
                    refresh = True
                if refresh:
                    self._refresh()
                    tokens = self._read()
            except (OSError, ValueError, KeyError, IndexError):
                raise AuthenticationUnavailable('Dedicated Codex sign-in is unavailable') from None
            return {'Authorization': 'Bearer ' + tokens['access_token'],
                    'ChatGPT-Account-ID': tokens['account_id']}

    def readiness(self):
        try:
            self._request_headers()
        except AuthenticationUnavailable:
            return {'ready': False, 'auth_mode': 'chatgpt', 'reason': 'sign_in_or_refresh_required'}
        return {'ready': True, 'auth_mode': 'chatgpt'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-home', required=True)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('action', choices=('login', 'status'))
    args = parser.parse_args(argv)
    owner = NativeAuthOwner(args.auth_home, args.codex)
    try:
        result = owner.login_device(lambda value: print(value, flush=True)) if args.action == 'login' else owner.readiness()
    except AuthenticationUnavailable as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
