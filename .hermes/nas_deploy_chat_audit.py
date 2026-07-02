import os
import posixpath
import shlex
import time
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

FILES=[]
for rel in ['requirements.txt','Dockerfile','docker-compose.yml','.dockerignore']:
    FILES.append((ROOT/rel, rel))
for base in ['app']:
    for p in (ROOT/base).rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            FILES.append((p, str(p.relative_to(ROOT)).replace('\\','/')))
for rel in ['data/storage/.gitkeep','data/backups/.gitkeep']:
    FILES.append((ROOT/rel, rel))


def run(client, cmd, timeout=300):
    stdin, stdout, stderr=client.exec_command(cmd, timeout=timeout)
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


def sftp_mkdirs(sftp, remote_dir):
    parts=[]
    cur=remote_dir
    while cur not in ('','/'):
        parts.append(cur)
        cur=posixpath.dirname(cur)
    for d in reversed(parts):
        try:
            sftp.stat(d)
        except IOError:
            sftp.mkdir(d)

client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    # create writable stack dir via docker-mounted helper because stacks dir is root-owned
    helper = (
        "sudo -n /usr/local/bin/docker run --rm "
        f"-v {shlex.quote(STACKS)}:/stacks python:3.11-slim "
        "sh -c " + shlex.quote(f"mkdir -p /stacks/chat-audit-core/data/storage /stacks/chat-audit-core/data/backups && chown -R {UID}:{GID} /stacks/chat-audit-core")
    )
    run(client, helper, timeout=120)
    sftp=client.open_sftp()
    try:
        for local, rel in FILES:
            remote_rel = 'compose.yaml' if rel == 'docker-compose.yml' else rel
            remote_path=posixpath.join(REMOTE, remote_rel)
            sftp_mkdirs(sftp, posixpath.dirname(remote_path))
            sftp.put(str(local), remote_path)
            print('PUT '+rel+' -> '+remote_path)
    finally:
        sftp.close()
    run(client, f"cd {shlex.quote(REMOTE)} && sudo -n /usr/local/bin/docker compose -f compose.yaml config", timeout=120)
    run(client, f"cd {shlex.quote(REMOTE)} && sudo -n /usr/local/bin/docker compose -f compose.yaml up -d --build", timeout=900)
    run(client, "sudo -n /usr/local/bin/docker ps --filter name=chat-audit --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'", timeout=120)
finally:
    client.close()
