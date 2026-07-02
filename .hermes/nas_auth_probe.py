import os
import paramiko

HOST = os.environ.get("NAS_HOST", "192.168.31.210")
PORT = int(os.environ.get("NAS_PORT", "77"))
USER = os.environ.get("NAS_USER", "YokiiroBW")
PASSWORD = os.environ.get("NAS_SSH_PASSWORD", "")

if not PASSWORD:
    raise SystemExit("NAS_SSH_PASSWORD is required")

sock = None
transport = None
try:
    sock = paramiko.proxy.ProxyCommand(None) if False else None
    transport = paramiko.Transport((HOST, PORT))
    transport.banner_timeout = 10
    transport.auth_timeout = 10
    transport.connect()
    print("REMOTE_VERSION=" + str(transport.remote_version))
    print("SECURITY_OPTIONS_KEX=" + ",".join(transport.get_security_options().kex[:3]))
    try:
        transport.auth_password(USER, PASSWORD, fallback=True)
        print("AUTH_METHOD=password_or_fallback")
    except Exception as exc:
        print("PASSWORD_AUTH_FAILED=" + exc.__class__.__name__ + ": " + str(exc))
        def handler(title, instructions, prompts):
            print("INTERACTIVE_TITLE=" + str(title))
            print("INTERACTIVE_INSTRUCTIONS=" + str(instructions))
            print("INTERACTIVE_PROMPTS=" + repr(prompts))
            return [PASSWORD for _prompt, _echo in prompts]
        transport.auth_interactive(USER, handler)
        print("AUTH_METHOD=keyboard_interactive")

    if not transport.is_authenticated():
        raise SystemExit("NOT_AUTHENTICATED")

    chan = transport.open_session(timeout=10)
    chan.exec_command("echo HERMES_SSH_OK; whoami; hostname; pwd; id")
    out = chan.makefile("rb", -1).read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb", -1).read().decode("utf-8", "replace")
    print(out.rstrip())
    if err:
        print("[stderr]")
        print(err.rstrip())
finally:
    if transport is not None:
        transport.close()
