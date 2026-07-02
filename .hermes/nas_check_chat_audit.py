import os
from pathlib import Path
import paramiko
HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
CMDS=[
 ('stack_ls','ls -la /volume1/Download/dockge/stacks/chat-audit-core 2>&1; find /volume1/Download/dockge/stacks/chat-audit-core -maxdepth 2 -type f | sort | head -n 80'),
 ('compose_ps','cd /volume1/Download/dockge/stacks/chat-audit-core 2>/dev/null && sudo -n /usr/local/bin/docker compose -f compose.yaml ps 2>&1 || true'),
 ('containers','sudo -n /usr/local/bin/docker ps -a --filter name=chat-audit --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1 || true'),
 ('images','sudo -n /usr/local/bin/docker images --format "{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}" | grep -E "chat-audit|<none>|python" | head -n 80 || true'),
 ('logs_app','sudo -n /usr/local/bin/docker logs --tail 80 chat-audit-core 2>&1 || true'),
 ('logs_db','sudo -n /usr/local/bin/docker logs --tail 40 chat-audit-postgres 2>&1 || true'),
 ('port','sudo -n /usr/local/bin/docker ps --format "{{.Names}} {{.Ports}}" | grep -E "chat-audit|:8000" || true'),
]
client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label,cmd in CMDS:
        print('===== '+label+' =====', flush=True)
        stdin,stdout,stderr=client.exec_command(cmd,timeout=120)
        out=stdout.read().decode('utf-8','replace').strip()
        err=stderr.read().decode('utf-8','replace').strip()
        code=stdout.channel.recv_exit_status()
        print('EXIT='+str(code), flush=True)
        if out: print(out, flush=True)
        if err: print('[stderr]\n'+err, flush=True)
finally:
    client.close()
