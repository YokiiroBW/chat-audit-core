import os
from pathlib import Path
import paramiko

HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
COMMANDS=[
 ('identity','echo HERMES_SSH_KEY_OK; whoami; id; hostname; pwd'),
 ('sudo_docker_version','sudo -n /usr/local/bin/docker --version 2>&1; sudo -n /usr/local/bin/docker compose version 2>&1'),
 ('sudo_docker_ps','sudo -n /usr/local/bin/docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1'),
 ('dockge_find','sudo -n /usr/local/bin/docker ps --format "{{.Names}} {{.Image}}" 2>/dev/null | grep -i dockge || true'),
 ('compose_ls','sudo -n /usr/local/bin/docker compose ls 2>&1 || true'),
 ('dockge_inspect','name=$(sudo -n /usr/local/bin/docker ps --format "{{.Names}}" 2>/dev/null | grep -i dockge | head -n 1); if [ -n "$name" ]; then sudo -n /usr/local/bin/docker inspect "$name" --format "NAME={{.Name}}\nIMAGE={{.Config.Image}}\nPORTS={{json .NetworkSettings.Ports}}\nMOUNTS={{json .Mounts}}\nENV={{json .Config.Env}}"; else echo NO_DOCKGE_CONTAINER_FOUND; fi'),
 ('dirs','for d in /opt/stacks /opt/dockge /volume1/docker/dockge /volume2/docker/dockge /volume1/docker /volume2/docker /volume2/homes/YokiiroBW; do [ -e "$d" ] && echo DIR=$d && ls -la "$d" 2>/dev/null | head -n 80; done'),
 ('stack_files','for d in /opt/stacks /volume1/docker/dockge /volume2/docker/dockge /volume1/docker /volume2/docker; do [ -e "$d" ] && echo SCAN=$d && find "$d" -maxdepth 5 \( -name docker-compose.yml -o -name compose.yml -o -name .env \) -print 2>/dev/null | head -n 120; done'),
]
client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label, cmd in COMMANDS:
        print('===== '+label+' =====', flush=True)
        stdin, stdout, stderr=client.exec_command(cmd, timeout=40)
        out=stdout.read().decode('utf-8','replace').strip()
        err=stderr.read().decode('utf-8','replace').strip()
        if out: print(out, flush=True)
        if err: print('[stderr]\n'+err, flush=True)
finally:
    client.close()
