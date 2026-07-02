import os
from pathlib import Path
import paramiko

HOST=os.environ.get('NAS_HOST','192.168.31.210')
PORT=int(os.environ.get('NAS_PORT','77'))
USER=os.environ.get('NAS_USER','YokiiroBW')
KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
COMMANDS=[
 ('paths','for d in /volume1/Download/dockge /volume1/Download/dockge/stacks /volume1/Download/dockge/stacks/dockge /work/stacks /opt/stacks /volume1/Download /volume1; do echo CHECK=$d; if [ -e "$d" ]; then ls -ld "$d"; ls -la "$d" 2>/dev/null | head -n 40; else echo MISSING; fi; done'),
 ('write_test','d=/volume1/Download/dockge/stacks/.hermes_write_test; rm -f "$d" 2>/dev/null; if echo ok > "$d" 2>/dev/null; then echo WRITE_OK; cat "$d"; rm -f "$d"; else echo WRITE_FAIL; fi'),
 ('port_8000','sudo -n /usr/local/bin/docker ps --format "{{.Names}} {{.Ports}}" | grep -E "(:8000->|0.0.0.0:8000|:::8000)" || true'),
 ('images_project','sudo -n /usr/local/bin/docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | grep -E "chat-audit|python" || true'),
]

client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
    for label,cmd in COMMANDS:
        print('===== '+label+' =====', flush=True)
        stdin, stdout, stderr=client.exec_command(cmd, timeout=60)
        out=stdout.read().decode('utf-8','replace').strip()
        err=stderr.read().decode('utf-8','replace').strip()
        if out: print(out, flush=True)
        if err: print('[stderr]\n'+err, flush=True)
finally:
    client.close()
