"""
ssh_client.py — Thin wrapper around Paramiko for running SSH commands on boardwalk's droplet.
"""

import paramiko
import io
from pathlib import Path


class SSHError(Exception):
    pass


def _get_client(cfg: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ip = cfg.get("droplet_ip", "")
    port = int(cfg.get("ssh_port", 22))
    user = cfg.get("ssh_user", "root")
    password = cfg.get("ssh_password", "") or None
    key_path = cfg.get("ssh_key_path", "") or None

    connect_kwargs = dict(
        hostname=ip,
        port=port,
        username=user,
        timeout=15,
        allow_agent=True,
        look_for_keys=True,
    )

    if key_path:
        key_path = str(Path(key_path).expanduser())
        try:
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
        except paramiko.ssh_exception.SSHException:
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
            except Exception:
                pkey = None
        if pkey:
            connect_kwargs["pkey"] = pkey

    if password:
        connect_kwargs["password"] = password

    try:
        client.connect(**connect_kwargs)
    except Exception as e:
        raise SSHError(f"Cannot connect to {ip}:{port} as {user}: {e}")

    return client


def run_ssh(cfg: dict, command: str, timeout: int = 30) -> str:
    """
    Run a shell command on the remote droplet and return stdout as a string.
    Raises SSHError on connection or execution failure.
    """
    client = _get_client(cfg)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out if out else err
    except Exception as e:
        raise SSHError(f"Command failed: {e}")
    finally:
        client.close()


def run_ssh_script(cfg: dict, script: str, timeout: int = 60) -> str:
    """
    Run a multi-line shell script on the remote droplet via stdin.
    Returns combined stdout + stderr.
    """
    client = _get_client(cfg)
    try:
        transport = client.get_transport()
        channel = transport.open_session()
        channel.exec_command("bash -s")
        channel.sendall(script.encode())
        channel.shutdown_write()

        output = b""
        while not channel.exit_status_ready():
            if channel.recv_ready():
                output += channel.recv(4096)
        while channel.recv_ready():
            output += channel.recv(4096)

        return output.decode("utf-8", errors="replace")
    except Exception as e:
        raise SSHError(f"Script execution failed: {e}")
    finally:
        client.close()
