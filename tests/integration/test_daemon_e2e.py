"""daemon 端到端：拉起 -> status -> connection.check -> 留痕 -> 停止。"""

import asyncio
import os
import shutil
import time
from pathlib import Path

import aiosqlite
import pytest

from clu.client import CluClient
from clu.config.store import ConfigStore, ConnectionConfig
from clu.constants import connections_path, ledger_path
from clu.daemon.server import daemon_alive, ensure_daemon

pytestmark = pytest.mark.integration


async def test_daemon_lifecycle_and_check(sshd: dict, tmp_path, monkeypatch) -> None:
    # macOS AF_UNIX 路径上限 ~104 字节，pytest tmp 目录过长，运行时用短路径
    rt = Path(f"/tmp/clu-it-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data"))

    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="t",
            kind="local",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )

    try:
        result = ensure_daemon()
        assert result["alive"] is True
        assert result["started"] is True

        async with CluClient() as client:
            status = await client.request("server.status")
            assert status["pid"] > 0
            check = await client.request("connection.check", {"label": "t"})
            assert check["exit_code"] == 0
            await client.request("server.shutdown")

        deadline = time.monotonic() + 5
        while daemon_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not daemon_alive()

        async with aiosqlite.connect(ledger_path()) as db:
            cursor = await db.execute("SELECT kind, connection FROM exec_records")
            rows = await cursor.fetchall()
        assert ("exec", "t") in rows
    finally:
        shutil.rmtree(rt, ignore_errors=True)
