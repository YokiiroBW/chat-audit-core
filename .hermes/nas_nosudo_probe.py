import os
import time
from pathlib import Path
import paramiko

HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path(os.environ.get('NAS_KEY_PATH', str(Path.home()/'.ssh'/'nas_192_168_31_210_ed25519')))
COMMANDS=[
 ('identity','echo HERMES_SSH_KEY_OK; whoami; hostname; pwd; uname -a; id'),
 ('docker_path','command -v docker || true; docker --version 2>&1 || true; docker compose version 2>&1 || true'),
 ('docker_socket','ls -l /var/run/docker.sock 2>&1 || true'),
 ('docker_ps','docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1 || true'),
 ('dockge_find','docker ps --format "{{.Names}} {{.Image}}" 2>/dev/null | grep -i dockge || true'),
 ('dirs','for d in /opt/stacks /opt/dockge /volume1/docker/dockge /volume2/docker/dockge /volume1/docker /volume2/docker /volume2/homes/YokiiroBW; do [ -e "$d" ] && echo DIR=$d && ls -la "$d" 2>/dev/null | head -n 80; done'),
]

def run(client,label,cmd,timeout=25):
    print('===== '+label+' =====', flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    chan=stdout.channel
    chan.settimeout(2)
    out=[]; err=[]; deadline=time.time()+timeout
    while True:
        if chan.recv_ready(): out.append(chan.recv(4096).decode('utf-8','replace'))
        if chan.recv_stderr_ready(): err.append(chan.recv_stderr(4096).decode('utf-8','replace'))
        if chan.exit_status_ready():
            while chan.recv_ready(): out.append(chan.recv(4096).decode('utf-8','replace'))
            while chan.recv_stderr_ready(): err.append(chan.recv_stderr(4096).decode('utf-8','replace'))
            break
        if time.time()>deadline:
            out.append('\n[TIMEOUT]\n'); chan.close(); break
        time.sleep(0.1)
    if out: print(''.join(out).strip(), flush=True)
    if err: print('[stderr]\n'+''.join(err).strip(), flush=True)

client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label,cmd in COMMANDS: run(client,label,cmd)
finally:
    client.close()
