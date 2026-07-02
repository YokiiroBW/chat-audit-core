import os
from pathlib import Path
import paramiko
HOST='192.168.31.210'; PORT=77; USER='YokiiroBW'; KEY_PATH=Path.home()/'.ssh'/'nas_192_168_31_210_ed25519'
CMDS=[
('ps','sudo -n /usr/local/bin/docker ps --filter name=chat-audit --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'),
('health_container','sudo -n /usr/local/bin/docker exec chat-audit-core python -c "import json, urllib.request; print(json.load(urllib.request.urlopen(\"http://127.0.0.1:8000/health\", timeout=3)))"'),
('routes','sudo -n /usr/local/bin/docker exec chat-audit-core python -c "from app.main import app; print([r.path for r in app.routes if hasattr(r, \"path\")])"'),
('logs','sudo -n /usr/local/bin/docker logs --tail 80 chat-audit-core 2>&1'),
]
client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
 key=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH)); client.connect(HOST,port=PORT,username=USER,pkey=key,timeout=10,banner_timeout=10,auth_timeout=10,look_for_keys=False,allow_agent=False)
 for label,cmd in CMDS:
  print('===== '+label+' =====', flush=True)
  stdin,stdout,stderr=client.exec_command(cmd,timeout=120); out=stdout.read().decode('utf-8','replace').strip(); err=stderr.read().decode('utf-8','replace').strip(); code=stdout.channel.recv_exit_status(); print('EXIT='+str(code));
  if out: print(out)
  if err: print('[stderr]\n'+err)
finally: client.close()
