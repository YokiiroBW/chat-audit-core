import os
import time
from pathlib import Path
import paramiko

HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
COMMANDS=[
 ('docker_paths','for p in docker /usr/local/bin/docker /usr/bin/docker /bin/docker /var/packages/ContainerManager/target/usr/bin/docker /var/packages/Docker/target/usr/bin/docker; do if command -v "$p" >/dev/null 2>&1 || [ -x "$p" ]; then echo FOUND=$p; "$p" --version 2>&1; "$p" compose version 2>&1 || true; fi; done'),
 ('packages','for d in /var/packages/ContainerManager /var/packages/Docker; do [ -e "$d" ] && echo DIR=$d && find "$d" -maxdepth 3 -type f -o -type l 2>/dev/null | head -n 80; done'),
 ('old_report','cat ~/hermes_docker_report.txt 2>/dev/null || true'),
 ('docker_try_absolute','/var/packages/ContainerManager/target/usr/bin/docker ps 2>&1 || true; /usr/local/bin/docker ps 2>&1 || true'),
 ('dockge_files_visible','for d in /opt/stacks /volume1/docker /volume2/docker /volume1/@docker /volume2/@docker; do [ -e "$d" ] && echo DIR=$d && find "$d" -maxdepth 4 -iname "*dockge*" -o -name docker-compose.yml -o -name compose.yml 2>/dev/null | head -n 120; done'),
]

def run(client,label,cmd,timeout=30):
    print('===== '+label+' =====', flush=True)
    stdin, stdout, stderr=client.exec_command(cmd, timeout=timeout)
    out=stdout.read().decode('utf-8','replace').strip()
    err=stderr.read().decode('utf-8','replace').strip()
    if out: print(out, flush=True)
    if err: print('[stderr]\n'+err, flush=True)

client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label,cmd in COMMANDS: run(client,label,cmd)
finally:
    client.close()
