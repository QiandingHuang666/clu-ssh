"""M2 集成：exec 安全门 + 假 slurm 的 jobs/status/submit/log/cancel + ledger。"""

import pytest

from clu.client import CluClient, CluError
from clu.config.store import ConfigStore, ConnectionConfig
from clu.constants import connections_path
from clu.daemon.server import ensure_daemon

pytestmark = pytest.mark.integration


async def test_slurm_tools_over_daemon(sshd: dict, tmp_path, monkeypatch) -> None:
    rt = __import__("pathlib").Path(f"/tmp/clu-it2-rt-{__import__('os').getpid()}")
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
        async with CluClient(timeout=60) as client:
            result = await client.request("exec", {"label": "cl", "command": "echo m2-hello"})
            assert result["exit_code"] == 0
            assert "m2-hello" in result["stdout"]

            with pytest.raises(CluError) as heavy:
                await client.request("exec", {"label": "cl", "command": "torchrun train.py"})
            assert heavy.value.code == "policy_denied"

            with pytest.raises(CluError) as danger:
                await client.request("exec", {"label": "cl", "command": "rm -rf /"})
            assert danger.value.code == "policy_denied"

            jobs = await client.request("slurm.jobs", {"label": "cl"})
            assert [j["job_id"] for j in jobs["jobs"]] == ["12345", "12346"]

            status = await client.request("slurm.status", {"label": "cl"})
            assert len(status["partitions"]) == 2
            assert status["queue"]["RUNNING"] == 1

            submitted = await client.request(
                "slurm.submit", {"label": "cl", "script": "#!/bin/bash\necho hi\n"}
            )
            assert submitted["job_id"] == "12345"

            log = await client.request("slurm.log", {"label": "cl", "job_id": "12345"})
            assert log["exists"] is True
            assert "fake-sbatch" in log["text"]

            cancelled = await client.request("slurm.cancel", {"label": "cl", "job_id": "12345"})
            assert cancelled["exit_code"] == 0

            ledger = await client.request("ledger.query", {"label_filter": None, "kind": "submit"})
            assert any(r["job_id"] == "12345" for r in ledger["records"])
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        __import__("shutil").rmtree(rt, ignore_errors=True)


async def test_resources_tool(sshd: dict, tmp_path, monkeypatch) -> None:
    """resources：节点级/分区级 CPU+GPU 聚合与过滤（假 slurm 数据）。"""
    import os
    import shutil
    from pathlib import Path

    from clu.config.store import ConfigStore, ConnectionConfig
    from clu.constants import connections_path
    from clu.daemon.server import ensure_daemon

    rt = Path(f"/tmp/clu-it7-rt-{os.getpid()}")
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg2"))
    monkeypatch.setenv("CLU_DATA_DIR", str(tmp_path / "data2"))
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
        async with CluClient(timeout=60) as client:
            result = await client.request("slurm.resources", {"label": "cl"})
            nodes = {n["node"]: n for n in result["nodes"]}
            assert nodes["gpu01"]["gpus"]["A100"]["free"] == 6
            assert nodes["gpu02"]["fully_idle"] is True
            assert result["partitions"]["gpu"]["gpus"]["A100"]["total"] == 16
            idle = await client.request("slurm.resources", {"label": "cl", "only_idle": True})
            assert {n["node"] for n in idle["nodes"]} == {"gpu02", "cpu01"}
    finally:
        async with CluClient() as client:
            await client.request("server.shutdown")
        shutil.rmtree(rt, ignore_errors=True)
