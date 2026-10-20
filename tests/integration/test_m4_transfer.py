"""M4 集成：SFTP 上传/下载 + 本地端口转发真通。"""

import os
import shutil
import socket
from pathlib import Path

import pytest

from clu.client import CluClient
from clu.config.store import ConfigStore, ConnectionConfig
from clu.constants import connections_path
from clu.daemon.server import ensure_daemon

pytestmark = pytest.mark.integration


async def _setup(sshd: dict, tmp_path, monkeypatch, tag: str) -> Path:
    rt = Path(f"/tmp/clu-it6-{tag}-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data"))
    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="cl",
            kind="instance",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )
    ensure_daemon()
    return rt


async def test_sftp_roundtrip(sshd: dict, tmp_path, monkeypatch) -> None:
    rt = await _setup(sshd, tmp_path, monkeypatch, "sftp")
    local = tmp_path / "hello.txt"
    local.write_text("clu-sftp-你好", encoding="utf-8")
    try:
        async with CluClient(timeout=120.0) as client:
            up = await client.request(
                "transfer.upload",
                {"label": "cl", "local_path": str(local), "remote_path": ".clu/uploaded/hello.txt"},
            )
            assert up["bytes"] == len("clu-sftp-你好".encode())

            result = await client.request(
                "exec", {"label": "cl", "command": "cat .clu/uploaded/hello.txt"}
            )
            assert "clu-sftp-你好" in result["stdout"]

            back = tmp_path / "back.txt"
            down = await client.request(
                "transfer.download",
                {"label": "cl", "remote_path": ".clu/uploaded/hello.txt", "local_path": str(back)},
            )
            assert down["bytes"] == up["bytes"]
            assert back.read_text(encoding="utf-8") == "clu-sftp-你好"

            ledger = await client.request("ledger.query", {"kind": "upload"})
            assert any("hello.txt" in str(r["command"]) for r in ledger["records"])
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)


async def test_forward_lifecycle(sshd: dict, tmp_path, monkeypatch) -> None:
    rt = await _setup(sshd, tmp_path, monkeypatch, "fwd")
    try:
        async with CluClient(timeout=60.0) as client:
            row = await client.request(
                "forward.add",
                {"label": "cl", "remote_host": "127.0.0.1", "remote_port": 22, "local_port": 0},
            )
            port = int(row["local_port"])
            assert port > 0

            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.settimeout(5)
                banner = sock.recv(64)
            assert banner.startswith(b"SSH-"), banner  # 经隧道打到容器 sshd

            listed = await client.request("forward.list", {})
            assert any(f["id"] == row["id"] for f in listed["forwards"])

            removed = await client.request("forward.remove", {"id": str(row["id"])})
            assert removed["removed"] is True
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)
