import os
from pathlib import Path
import paramiko
HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
CMDS=[
 ('compose_config','cd /volume1/Download/dockge/stacks/chat-audit-core && sudo -n /usr/local/bin/docker compose -f compose.yaml config 2>&1'),
 ('compose_up','cd /volume1/Download/dockge/stacks/chat-audit-core && sudo -n /usr/local/bin/docker compose -f compose.yaml up -d --build 2>&1'),
 ('compose_ps_after','cd /volume1/Download/dockge/stacks/chat-audit-core && sudo -n /usr/local/bin/docker compose -f compose.yaml ps 2>&1'),
 ('containers_after','sudo -n /usr/local/bin/docker ps -a --filter name=chat-audit --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1'),
]
client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label,cmd in CMDS:
        print('===== '+label+' =====', flush=True)
        stdin,stdout,stderr=client.exec_command(cmd,timeout=900)
        out=stdout.read().decode('utf-8','replace').strip()
        err=stderr.read().decode('utf-8','replace').strip()
        code=stdout.channel.recv_exit_status()
        print('EXIT='+str(code), flush=True)
        if out: print(out[-12000:], flush=True)
        if err: print('[stderr]\n'+err[-12000:], flush=True)
        if code != 0:
            break
finally:
    client.close()
