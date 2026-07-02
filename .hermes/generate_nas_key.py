from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ssh_dir = Path.home() / ".ssh"
ssh_dir.mkdir(parents=True, exist_ok=True)
key_path = ssh_dir / "nas_192_168_31_210_ed25519"
pub_path = ssh_dir / "nas_192_168_31_210_ed25519.pub"

if key_path.exists() or pub_path.exists():
    print(f"EXISTS_PRIVATE={key_path}")
    print(f"EXISTS_PUBLIC={pub_path}")
    raise SystemExit(0)

private_key = ed25519.Ed25519PrivateKey.generate()
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.OpenSSH,
    encryption_algorithm=serialization.NoEncryption(),
)
public_bytes = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.OpenSSH,
    format=serialization.PublicFormat.OpenSSH,
)
comment = b" hermes-nas-192.168.31.210"

key_path.write_bytes(private_bytes)
pub_path.write_bytes(public_bytes + comment + b"\n")
print(f"CREATED_PRIVATE={key_path}")
print(f"CREATED_PUBLIC={pub_path}")
