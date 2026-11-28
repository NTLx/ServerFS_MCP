#!/usr/bin/env python3
"""One-shot SO_PEERCRED probe for the serverfs-mcp container identity.

Run as the normal ServerFS user. The probe socket lives below the user's
persistent ServerFS Agent deployment tree and is intentionally temporary. No
root privilege, system directory or host ownership change is required.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import stat
import struct
from pathlib import Path


def _default_directory() -> Path:
    home = Path.home()
    if not home.is_absolute():
        raise SystemExit("user home must be absolute")
    return home / ".local/share/serverfs-agent-bridge/peer-probe"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()

    if os.geteuid() == 0:
        raise SystemExit("run the Phase E peer probe as the normal login user, not root")

    directory = args.directory or _default_directory()
    if not directory.is_absolute():
        raise SystemExit("probe directory must be absolute")

    created_dir = False
    try:
        try:
            st = directory.lstat()
        except FileNotFoundError:
            # The probe runs before install.sh in the documented order, so the
            # parent deployment tree may not exist yet; only the probe directory
            # this process creates is removed again on the way out.
            directory.mkdir(mode=0o755, parents=True)
            created_dir = True
            st = directory.lstat()

        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise SystemExit("probe directory must be a real directory")
        if st.st_uid != os.getuid():
            raise SystemExit("probe directory must be owned by the current user")
        if st.st_mode & 0o022:
            raise SystemExit("probe directory must not be group/world writable")
        os.chmod(directory, 0o755)

        socket_path = directory / "peer.sock"
        try:
            socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise SystemExit("refusing to replace an existing probe socket path")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_path))
            # The random token authenticates this one-shot measurement.
            os.chmod(socket_path, 0o666)
            server.listen(4)
            token = secrets.token_urlsafe(24)
            print(f"listening={socket_path}", flush=True)
            print(f"token={token}", flush=True)

            for _ in range(20):
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(5)
                    received = b""
                    while b"\n" not in received and len(received) <= 256:
                        chunk = connection.recv(256 - len(received) + 1)
                        if not chunk:
                            break
                        received += chunk
                    if received.rstrip(b"\r\n").decode("utf-8", errors="replace") != token:
                        continue
                    raw = connection.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        struct.calcsize("3i"),
                    )
                    pid, uid, gid = struct.unpack("3i", raw)
                    print(f"pid={pid}")
                    print(f"uid={uid}")
                    print(f"gid={gid}")
                    print(f"login_uid={os.getuid()}")
                    print(f"login_gid={os.getgid()}")
                    compatible = uid == os.getuid() and gid == os.getgid()
                    print(f"user_scope_compatible={str(compatible).lower()}")
                    if not compatible:
                        raise SystemExit(
                            "container peer identity differs from the login user; "
                            "Phase E user-scoped deployment must stop rather than "
                            "request privileged ownership changes"
                        )
                    break
            else:
                raise SystemExit("no authenticated probe connection was received")
        finally:
            server.close()
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
    finally:
        if created_dir:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    main()
