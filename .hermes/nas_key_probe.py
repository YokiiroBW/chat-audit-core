import os
import socket
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("NAS_HOST", "192.168.31.210")
PORT = int(os.environ.get("NAS_PORT", "77"))
USER = os.environ.get("NAS_USER", "YokiiroBW")
KEY_PATH = Path(os.environ.get("NAS_KEY_PATH", str(Path.home() / ".ssh" / "nas_192_168_31_210_ed25519")))
SUDO_PASSWORD = os.environ.get("NAS_SUDO_PASSWORD", "")

COMMANDS = [
    ("identity", "echo HERMES_SSH_KEY_OK; whoami; hostname; pwd; uname -a; id", False),
    ("docker_path_no_sudo", "command -v docker || true; /usr/local/bin/docker --version 2>&1 || docker --version 2>&1 || true; docker compose version 2>&1 || true", False),
    ("docker_socket", "ls -l /var/run/docker.sock 2>&1 || true", False),
    ("docker_ps_no_sudo", "docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}' 2>&1 || true", False),
    ("sudo_noninteractive", "sudo -n sh -c 'echo HERMES_SUDO_N_OK; whoami; id' 2>&1 || true", False),
    ("sudo_check", "sudo -S -p '' sh -c 'echo HERMES_SUDO_OK; whoami; id'", True),
    ("docker_path_sudo", "sudo -S -p '' sh -c 'command -v docker || true; docker --version 2>&1 || true; docker compose version 2>&1 || true'", True),
    ("docker_ps_sudo", "sudo -S -p '' sh -c \"docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}' 2>&1 || true\"", True),
    ("dockge_find_sudo", "sudo -S -p '' sh -c \"docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i dockge || true\"", True),
    ("dockge_inspect_sudo", "sudo -S -p '' sh -c \"name=\\$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i dockge | head -n 1); if [ -n \\\"\\$name\\\" ]; then docker inspect \\\"\\$name\\\" --format 'NAME={{.Name}}\\nIMAGE={{.Config.Image}}\\nPORTS={{json .NetworkSettings.Ports}}\\nMOUNTS={{json .Mounts}}\\nENV={{json .Config.Env}}'; fi\"", True),
    ("compose_ls_sudo", "sudo -S -p '' sh -c 'docker compose ls 2>&1 || true'", True),
    ("dockge_dirs_sudo", "sudo -S -p '' sh -c 'for d in /opt/stacks /opt/dockge /volume1/docker/dockge /volume2/docker/dockge /volume1/docker /volume2/docker /volume1/homes /volume2/homes /root; do [ -e \"$d\" ] && echo DIR=$d && ls -la \"$d\" 2>/dev/null | head -n 120; done'", True),
    ("stack_files_sudo", "sudo -S -p '' sh -c 'for d in /opt/stacks /volume1/docker/dockge /volume2/docker/dockge /volume1/docker /volume2/docker; do [ -e \"$d\" ] && find \"$d\" -maxdepth 5 \\( -name docker-compose.yml -o -name compose.yml -o -name .env \\) -print 2>/dev/null | head -n 120; done'", True),
]


def run(client, label, command, use_sudo_password=False, timeout=45):
    print(f"===== {label} =====")
    chan = client.get_transport().open_session(timeout=10)
    if use_sudo_password:
        chan.get_pty()
    chan.settimeout(2)
    chan.exec_command(command)
    if use_sudo_password and SUDO_PASSWORD:
        chan.send(SUDO_PASSWORD + "\n")
    out_parts = []
    err_parts = []
    deadline = time.time() + timeout
    while True:
        while chan.recv_ready():
            out_parts.append(chan.recv(4096).decode("utf-8", "replace"))
        while chan.recv_stderr_ready():
            err_parts.append(chan.recv_stderr(4096).decode("utf-8", "replace"))
        if chan.exit_status_ready():
            while chan.recv_ready():
                out_parts.append(chan.recv(4096).decode("utf-8", "replace"))
            while chan.recv_stderr_ready():
                err_parts.append(chan.recv_stderr(4096).decode("utf-8", "replace"))
            break
        if time.time() > deadline:
            out_parts.append("\n[TIMEOUT]\n")
            chan.close()
            break
        time.sleep(0.1)
    out = "".join(out_parts).strip()
    err = "".join(err_parts).strip()
    if SUDO_PASSWORD:
        out = out.replace(SUDO_PASSWORD, "***")
        err = err.replace(SUDO_PASSWORD, "***")
    if out:
        print(out)
    if err:
        print("[stderr]")
        print(err)


key = paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(HOST, port=PORT, username=USER, pkey=key, timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
    for label, command, use_sudo in COMMANDS:
        run(client, label, command, use_sudo)
finally:
    client.close()
