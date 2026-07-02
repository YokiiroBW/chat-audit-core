import os

import paramiko

HOST = os.environ.get("NAS_HOST", "192.168.31.210")
PORT = int(os.environ.get("NAS_PORT", "77"))
USER = os.environ.get("NAS_USER", "YokiiroBW")
PASSWORD = os.environ.get("NAS_SSH_PASSWORD")
SUDO_PASSWORD = os.environ.get("NAS_SUDO_PASSWORD", PASSWORD or "")

if not PASSWORD:
    raise SystemExit("NAS_SSH_PASSWORD is required")

COMMANDS = [
    ("identity", "echo HERMES_SSH_OK; whoami; hostname; pwd; uname -a; id"),
    ("sudo_check", "sudo -S -p '' sh -c 'echo HERMES_SUDO_OK; whoami; id'"),
    ("docker_path", "sudo -S -p '' sh -c 'command -v docker || true; docker --version 2>&1 || true; docker compose version 2>&1 || true'"),
    ("docker_ps", "sudo -S -p '' sh -c \"docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}' 2>&1 || true\""),
    ("dockge_find", "sudo -S -p '' sh -c \"docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i dockge || true\""),
    ("dockge_inspect", "sudo -S -p '' sh -c \"name=\\$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i dockge | head -n 1); if [ -n \\\"\\$name\\\" ]; then docker inspect \\\"\\$name\\\" --format 'NAME={{.Name}}\\nIMAGE={{.Config.Image}}\\nPORTS={{json .NetworkSettings.Ports}}\\nMOUNTS={{json .Mounts}}\\nENV={{json .Config.Env}}'; fi\""),
    ("dockge_dirs", "sudo -S -p '' sh -c 'for d in /opt/stacks /opt/dockge /volume1/docker/dockge /volume1/docker /volume1/homes /root; do [ -e \"$d\" ] && echo DIR=$d && ls -la \"$d\" 2>/dev/null | head -n 80; done'"),
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(
        HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    for label, command in COMMANDS:
        print(f"===== {label} =====")
        stdin, stdout, stderr = client.exec_command(command, timeout=40)
        if command.startswith("sudo "):
            stdin.write(SUDO_PASSWORD + "\n")
            stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if out:
            print(out.rstrip())
        if err:
            print("[stderr]")
            print(err.rstrip())
finally:
    client.close()
