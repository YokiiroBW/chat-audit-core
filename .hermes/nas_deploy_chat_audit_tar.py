import base64
import io
import os
import posixpath
import shlex
import tarfile
from pathlib import Path

import paramiko

HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
ROOT=Path(r'C:\Users\Administrator\Documents\Hermes\QQWXTB')
REMOTE='/volume1/Download/dockge/stacks/chat-audit-core'
STACKS='/volume1/Download/dockge/stacks'
UID='1026'
GID='100'

INCLUDE=[]
for rel in ['requirements.txt','Dockerfile','.dockerignore']:
    INCLUDE.append((ROOT/rel, rel))
INCLUDE.append((ROOT/'docker-compose.yml', 'compose.yaml'))
for base in ['app']:
    for p in (ROOT/base).rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            INCLUDE.append((p, str(p.relative_to(ROOT)).replace('\\','/')))
for rel in ['data/storage/.gitkeep','data/backups/.gitkeep']:
    INCLUDE.append((ROOT/rel, rel))


def run(client, cmd, timeout=300, input_bytes=None):
    stdin, stdout, stderr=client.exec_command(cmd, timeout=timeout)
    if input_bytes is not None:
        stdin.write(input_bytes)
        stdin.channel.shutdown_write()
    out=stdout.read().decode('utf-8','replace')
    err=stderr.read().decode('utf-8','replace')
    code=stdout.channel.recv_exit_status()
    print('CMD='+cmd)
    print('EXIT='+str(code))
    if out.strip(): print(out.rstrip())
    if err.strip(): print('[stderr]\n'+err.rstrip())
    if code != 0:
        raise SystemExit(code)
    return out


def make_archive_b64():
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for local, arc in INCLUDE:
            info=tar.gettarinfo(str(local), arcname=arc)
            info.uid=UID and int(UID)
            info.gid=GID and int(GID)
            info.uname=USER
            info.gname='users'
            with local.open('rb') as f:
                tar.addfile(info, f)
    return base64.b64encode(buf.getvalue())

client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    helper=(
        'sudo -n /usr/local/bin/docker run --rm '
        f'-v {shlex.quote(STACKS)}:/stacks python:3.11-slim '
        'sh -c '+shlex.quote(f'rm -rf /stacks/chat-audit-core && mkdir -p /stacks/chat-audit-core/data/storage /stacks/chat-audit-core/data/backups && chown -R {UID}:{GID} /stacks/chat-audit-core')
    )
    run(client, helper, timeout=180)
    archive_b64=make_archive_b64()
    print(f'UPLOAD_ARCHIVE_BASE64_BYTES={len(archive_b64)}')
    run(client, f'base64 -d | tar -xzf - -C {shlex.quote(REMOTE)}', timeout=180, input_bytes=archive_b64)
    run(client, f'cd {shlex.quote(REMOTE)} && sudo -n /usr/local/bin/docker compose -f compose.yaml config', timeout=180)
    run(client, f'cd {shlex.quote(REMOTE)} && sudo -n /usr/local/bin/docker compose -f compose.yaml up -d --build', timeout=900)
    run(client, "sudo -n /usr/local/bin/docker ps --filter name=chat-audit --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'", timeout=120)
finally:
    client.close()
