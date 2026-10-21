"""M3 集成：真 tmux + 假 salloc —— alloc 全链路（授予/保活/列表/释放/等待超时）。"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from clu.client import CluClient, CluError
from clu.config.store import ConfigStore, ConnectionConfig
from clu.constants import connections_path
from clu.daemon.server import ensure_daemon

pytestmark = pytest.mark.integration


async def test_alloc_lifecycle(sshd: dict, tmp_path, monkeypatch) -> None:
    rt = Path(f"/tmp/clu-it4-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data"))

    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="cl",
            kind="cluster",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )
    ensure_daemon()
    try:
        async with CluClient(timeout=180.0) as client:
            # 授予路径：假 salloc 1s 后授予 12345
            info = await client.request(
                "slurm.alloc",
                {"label": "cl", "purpose": "train", "partition": "gpu", "gpus": 1, "wait": 30},
            )
            assert info["state"] == "granted", info
            assert info["job_id"] == "12345"
            assert info["node"] == "cpu48c-4"

            # 会话存活（sleep infinity 保活）
            result = await client.request(
                "exec", {"label": "cl", "command": "tmux has-session -t clu/cl/train"}
            )
            assert result["exit_code"] == 0

            # 列表与详情
            listed = await client.request("session.list", {"label": "cl"})
            purposes = [s["purpose"] for s in listed["sessions"]]
            assert "train" in purposes
            detail = await client.request("session.info", {"label": "cl", "purpose": "train"})
            assert detail["job_id"] == "12345"

            # 释放
            released = await client.request("session.release", {"label": "cl", "purpose": "train"})
            assert released["released"] is True
            result = await client.request(
                "exec", {"label": "cl", "command": "tmux has-session -t clu/cl/train"}
            )
            assert result["exit_code"] != 0
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)


async def test_alloc_pending_keeps_session(sshd: dict, tmp_path, monkeypatch) -> None:
    """等待超时的会话保留在 tmux（慢分配场景），随后可释放。"""
    rt = Path(f"/tmp/clu-it5-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data"))

    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="cl",
            kind="cluster",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )
    ensure_daemon()
    try:
        async with CluClient(timeout=120.0) as client:
            # 标记文件让假 salloc 延迟 60s（等不到授予）
            await client.request("exec", {"label": "cl", "command": "touch ~/fake-slow"})
            info = await client.request(
                "slurm.alloc", {"label": "cl", "purpose": "slow", "wait": 4}
            )
            assert info["state"] == "pending"
            assert "session_info" in str(info.get("note"))
            released = await client.request("session.release", {"label": "cl", "purpose": "slow"})
            assert released["released"] is True
            await client.request("exec", {"label": "cl", "command": "rm -f ~/fake-slow"})
    except CluError:
        pytest.fail("alloc 慢分配链路异常")
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)


async def test_session_survives_daemon_restart(sshd: dict, tmp_path, monkeypatch) -> None:
    """本地 daemon 重启，远端资源仍在：登记表让会话信息零探测恢复。"""
    rt = Path(f"/tmp/clu-it8-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg3"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data3"))
    store = ConfigStore(connections_path())
    store.add_connection(
        ConnectionConfig(
            label="cl",
            kind="cluster",
            host=str(sshd["host"]),
            port=int(sshd["port"]),
            user="tester",
            identity=str(sshd["identity"]),
        )
    )
    ensure_daemon()
    try:
        async with CluClient(timeout=120) as client:
            info = await client.request(
                "slurm.alloc", {"label": "cl", "purpose": "restart", "partition": "gpu", "wait": 30}
            )
            assert info["state"] == "granted"

        # 本地重启：socket 停止 -> 重新拉起（远端 tmux 与分配不动）
        await __import__("clu.daemon.lifecycle", fromlist=["stop_daemon"]).stop_daemon()
        import time as _t

        deadline = _t.monotonic() + 5
        while _t.monotonic() < deadline:
            from clu.daemon.server import daemon_alive

            if not daemon_alive():
                break
            await asyncio.sleep(0.1)
        ensure_daemon()

        async with CluClient(timeout=60) as client:
            listed = await client.request("session.list", {"label": "cl"})
            match = [s for s in listed["sessions"] if s["purpose"] == "restart"]
            assert match, listed
            row = match[0]
            assert row["job_id"] == "12345"  # 登记表直接给出，不靠重新解析日志
            assert row.get("verified") is True

            released = await client.request(
                "session.release", {"label": "cl", "purpose": "restart"}
            )
            assert released["released"] is True
            listed2 = await client.request("session.list", {"label": "cl"})
            assert not [s for s in listed2["sessions"] if s["purpose"] == "restart"]  # 销账
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)
