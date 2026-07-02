import os
import paramiko

HOST = os.environ.get("NAS_HOST", "192.168.31.210")
PORT = int(os.environ.get("NAS_PORT", "77"))
PASSWORD = os.environ.get("NAS_SSH_PASSWORD", "")
USERS = ["YokiiroBW", "yokiirobw", "yokiiroBW", "YOKIIROBW", "admin", "root"]

if not PASSWORD:
    raise SystemExit("NAS_SSH_PASSWORD is required")

for user in USERS:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST,
            port=PORT,
            username=user,
            password=PASSWORD,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command("echo HERMES_SSH_OK; whoami; hostname; pwd; id", timeout=10)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        print(f"USER={user} AUTH=OK")
        print(out)
        if err:
            print("STDERR=" + err)
        break
    except Exception as exc:
        print(f"USER={user} AUTH=FAIL {exc.__class__.__name__}: {exc}")
    finally:
        client.close()
