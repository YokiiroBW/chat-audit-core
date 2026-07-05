#!/usr/bin/env python3
"""Validate repository connectivity through SSH key + token-backed HTTPS.

The script checks:
- SSH transport: git ls-remote via SSH private key
- HTTPS transport: Forgejo token -> API probe + git ls-remote with temporary git config
- Optional reconcile: remote/main vs local/HEAD

Usage:
  python3 scripts/git_connectivity_check.py --remote origin
  python3 scripts/git_connectivity_check.py --skip-ssh
  FORGEJO_TOKEN_FILE=/path/to/token python3 scripts/git_connectivity_check.py
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Tuple
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_TOKEN_FILE = pathlib.Path('/opt/data/.hermes/private/forgejo_tokens/chat-audit-core.token')
DEFAULT_SSH_KEY = pathlib.Path.home() / '.ssh' / 'id_ed25519_forgejo'
DEFAULT_SSH_PORT = int(os.environ.get('FORGEJO_SSH_PORT', '2222'))
DEFAULT_REMOTE = 'origin'


@dataclass
class CheckResult:
    ok: bool
    message: str
    sample: str | None = None


def _run(cmd: list[str], cwd: pathlib.Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
    )


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _remote_url(repo_dir: pathlib.Path, remote_name: str) -> str:
    proc = _run(['git', 'remote', 'get-url', remote_name], repo_dir)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f'cannot read remote {remote_name}')
    return proc.stdout.strip()


def _split_http_remote(remote_url: str) -> Tuple[str, str, str]:
    p = urlparse(remote_url)
    if p.scheme not in {'http', 'https'}:
        raise ValueError(f'unsupported scheme for HTTPS check: {p.scheme}')
    if not p.netloc:
        raise ValueError('missing HTTP(S) host')
    repo = p.path.lstrip('/')
    if repo.endswith('.git'):
        repo = repo[:-4]
    if not repo:
        raise ValueError('missing repository path in remote')
    return p.scheme, p.netloc, repo


def _build_ssh_url(remote_url: str, ssh_port: int) -> str:
    p = urlparse(remote_url)

    if p.scheme == 'ssh':
        return remote_url

    if p.scheme in {'http', 'https'}:
        if not p.hostname:
            raise ValueError('cannot parse SSH host from HTTP(S) remote')
        path = p.path.lstrip('/')
        if path.endswith('.git'):
            path = path[:-4]
        return f'ssh://git@{p.hostname}:{ssh_port}/{path}'

    if remote_url.startswith('git@') and ':' in remote_url and '://' not in remote_url:
        # scp-like syntax: git@host:owner/repo.git
        host_part, path_part = remote_url.split(':', 1)
        host = host_part.split('@')[-1]
        path = path_part.lstrip('/')
        if path.endswith('.git'):
            path = path[:-4]
        return f'ssh://git@{host}:{ssh_port}/{path}'

    raise ValueError('unsupported remote format for SSH check')


def _prepare_ssh_env(key_path: pathlib.Path, ssh_port: int) -> dict[str, str]:
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    if sys.platform == 'win32':
        git_ssh_dir = pathlib.Path('C:/Program Files/Git/usr/bin')
        if git_ssh_dir.exists():
            env['PATH'] = f'{git_ssh_dir};{env.get("PATH", "")}'
        key_for_ssh = str(key_path).replace('\\', '/')
    else:
        key_for_ssh = str(key_path)
    env['GIT_SSH_COMMAND'] = (
        f'ssh -i "{key_for_ssh}" '
        '-o IdentitiesOnly=yes '
        '-o StrictHostKeyChecking=accept-new '
        f'-p {ssh_port}'
    )
    return env


def _check_ssh(
    repo_dir: pathlib.Path,
    remote_url: str,
    key_path: pathlib.Path,
    ssh_port: int,
) -> CheckResult:
    if not key_path.exists():
        return CheckResult(False, f'ssh-key missing: {key_path}')

    try:
        ssh_url = _build_ssh_url(remote_url, ssh_port)
    except Exception as exc:
        return CheckResult(False, f'ssh check skipped: {exc}')

    env = _prepare_ssh_env(key_path, ssh_port)
    proc = _run(['git', 'ls-remote', '--heads', ssh_url], repo_dir, env=env)
    if proc.returncode == 0 and proc.stdout.strip():
        first = proc.stdout.splitlines()[0] if proc.stdout else ''
        return CheckResult(True, f'ssh ok: {ssh_url}', first)

    detail = proc.stderr.strip() or proc.stdout.strip() or 'unknown error'
    return CheckResult(False, f'ssh failed: {ssh_url}', detail)


def _check_https(repo_dir: pathlib.Path, remote_url: str, token_file: pathlib.Path) -> CheckResult:
    try:
        scheme, host_port, repo = _split_http_remote(remote_url)
    except Exception as exc:
        return CheckResult(False, f'https check skipped: {exc}')

    if not token_file.exists():
        return CheckResult(False, f'token file missing: {token_file}')

    token = token_file.read_text().strip()
    if not token:
        return CheckResult(False, f'token file empty: {token_file}')

    # API probe
    api_url = f'{scheme}://{host_port}/api/v1/repos/{repo}'
    req = Request(
        api_url,
        headers={
            'Authorization': f'token {token}',
            'Accept': 'application/json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return CheckResult(False, f'https api failed: status={resp.status}')
    except URLError as exc:
        return CheckResult(False, f'https api request failed: {exc}')

    # git ls-remote with temporary extraheader config
    git_url = f'{scheme}://{host_port}/{repo}.git'
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_CONFIG_COUNT'] = '1'
    env['GIT_CONFIG_KEY_0'] = 'http.extraheader'
    env['GIT_CONFIG_VALUE_0'] = f'Authorization: token {token}'

    proc = _run(['git', 'ls-remote', '--heads', git_url], repo_dir, env=env)
    if proc.returncode == 0 and proc.stdout.strip():
        first = proc.stdout.splitlines()[0] if proc.stdout else ''
        return CheckResult(True, f'https ok: {git_url}', first)

    detail = proc.stderr.strip() or proc.stdout.strip() or 'unknown error'
    return CheckResult(False, f'https git check failed: {git_url}', detail)


def _git_auth_env_for_remote(remote_url: str, token_file: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'

    try:
        _scheme, _host_port, _repo = _split_http_remote(remote_url)
    except Exception:
        return env

    if not token_file.exists():
        return env

    token = token_file.read_text().strip()
    if not token:
        return env

    env['GIT_CONFIG_COUNT'] = '1'
    env['GIT_CONFIG_KEY_0'] = 'http.extraheader'
    env['GIT_CONFIG_VALUE_0'] = f'Authorization: token {token}'
    return env


def _reconcile(repo_dir: pathlib.Path, remote: str, remote_url: str, token_file: pathlib.Path) -> CheckResult:
    env = _git_auth_env_for_remote(remote_url, token_file)
    proc_fetch = _run(['git', 'fetch', '--prune', remote], repo_dir, env=env)
    if proc_fetch.returncode != 0:
        return CheckResult(False, 'reconcile: fetch failed', proc_fetch.stderr.strip() or proc_fetch.stdout.strip())

    proc_rev = _run(['git', 'rev-list', '--left-right', '--count', f'{remote}/main...HEAD'], repo_dir)
    if proc_rev.returncode != 0:
        return CheckResult(False, 'reconcile: rev-list failed', proc_rev.stderr.strip() or proc_rev.stdout.strip())

    counts = proc_rev.stdout.strip()
    parts = counts.replace('\t', ' ').split()
    if len(parts) == 2:
        return CheckResult(True, f'reconcile: behind={parts[0]} ahead={parts[1]}', counts)
    return CheckResult(True, f'reconcile: {counts}', counts)


def _print_result(label: str, result: CheckResult) -> None:
    status = 'PASS' if result.ok else 'FAIL'
    print(f'[{label}] {status}: {result.message}')
    if result.sample:
        print(f'[{label}] sample={result.sample}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--remote', default=DEFAULT_REMOTE)
    parser.add_argument('--ssh-key', default=os.environ.get('GIT_SSH_KEY', str(DEFAULT_SSH_KEY)))
    parser.add_argument('--ssh-port', type=int, default=DEFAULT_SSH_PORT)
    parser.add_argument('--token-file', default=os.environ.get('FORGEJO_TOKEN_FILE', str(DEFAULT_TOKEN_FILE)))
    parser.add_argument('--skip-ssh', action='store_true')
    parser.add_argument('--skip-https', action='store_true')
    parser.add_argument('--reconcile', action='store_true')
    args = parser.parse_args()

    repo = _repo_root()
    if not (repo / '.git').is_dir():
        print('local-error: not a git repository')
        return 2

    try:
        remote_url = _remote_url(repo, args.remote)
    except Exception as exc:
        print(f'local-error: {exc}')
        return 2

    if not remote_url:
        print('local-error: remote URL empty')
        return 2

    ok_ssh = True
    ok_https = True

    if args.skip_ssh:
        print('[SSH] SKIP: --skip-ssh')
        ok_ssh = True
    else:
        ssh_res = _check_ssh(repo, remote_url, pathlib.Path(args.ssh_key), args.ssh_port)
        _print_result('SSH', ssh_res)
        ok_ssh = ssh_res.ok

    if args.skip_https:
        print('[HTTPS] SKIP: --skip-https')
        ok_https = True
    else:
        https_res = _check_https(repo, remote_url, pathlib.Path(args.token_file))
        _print_result('HTTPS', https_res)
        ok_https = https_res.ok

    if args.reconcile and (ok_ssh or ok_https):
        rec = _reconcile(repo, args.remote, remote_url, pathlib.Path(args.token_file))
        _print_result('RECONCILE', rec)
        if not rec.ok:
            ok_https = ok_https and False

    print(f'summary_ssh={ok_ssh}')
    print(f'summary_https={ok_https}')
    return 0 if ok_ssh and ok_https else 1


if __name__ == '__main__':
    raise SystemExit(main())
