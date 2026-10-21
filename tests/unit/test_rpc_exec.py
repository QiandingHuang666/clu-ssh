"""RPC 层测试：exec 安全门接入、not_found、kind 约束（fake pool）。"""

from pathlib import Path

import pytest

from clu.config.store import ConfigStore, ConnectionConfig
from clu.daemon.rpc import DaemonContext, RpcFailure, dispatcher
from clu.ledger.db import Ledger
from clu.ssh.connection import ExecResult


class FakePool:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, label: str, command: str, *, timeout: float | None = None) -> ExecResult:
        self.commands.append(command)
        return ExecResult(command, 0, f"ran:{command}", "", 3)

    def status(self) -> list[dict[str, object]]:
        return []


async def make_ctx(tmp_path: Path) -> DaemonContext:
    store = ConfigStore(tmp_path / "connections.toml")
    store.add_connection(ConnectionConfig(label="gzu", kind="cluster", host="h", user="u"))
    store.add_connection(ConnectionConfig(label="box", kind="instance", host="h", user="u"))
    ledger = Ledger(tmp_path / "l.sqlite3")
    await ledger.open()
    return DaemonContext(
        store=store, pool=FakePool(), ledger=ledger, socket_path=tmp_path / "s.sock"
    )


async def test_exec_ok_records_ledger(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    result = await dispatcher.dispatch(ctx, "exec", {"label": "gzu", "command": "squeue -u u"})
    assert result["exit_code"] == 0
    assert result["stdout"] == "ran:squeue -u u"
    rows = await ctx.ledger.query(
        __import__("clu.ledger.query", fromlist=["LedgerFilter"]).LedgerFilter(kind="exec")
    )
    assert rows and rows[0]["connection"] == "gzu"


async def test_exec_heavy_denied_on_cluster(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    with pytest.raises(RpcFailure) as heavy:
        await dispatcher.dispatch(ctx, "exec", {"label": "gzu", "command": "torchrun a.py"})
    assert heavy.value.code == "policy_denied"
    assert "登录节点" in heavy.value.message
    with pytest.raises(RpcFailure) as danger:
        await dispatcher.dispatch(ctx, "exec", {"label": "gzu", "command": "rm -rf /"})
    assert danger.value.code == "policy_denied"


async def test_exec_heavy_allowed_on_instance(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    result = await dispatcher.dispatch(ctx, "exec", {"label": "box", "command": "torchrun a.py"})
    assert result["exit_code"] == 0


async def test_exec_unknown_label(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    with pytest.raises(RpcFailure, match="未知连接"):
        await dispatcher.dispatch(ctx, "exec", {"label": "ghost", "command": "ls"})


async def test_slurm_requires_cluster_kind(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    with pytest.raises(RpcFailure, match="kind=instance"):
        await dispatcher.dispatch(ctx, "slurm.jobs", {"label": "box"})


async def test_unknown_method(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    with pytest.raises(RpcFailure, match="未知方法"):
        await dispatcher.dispatch(ctx, "nope", {})


async def test_session_list_best_effort(tmp_path: Path) -> None:
    """一台连接不可达只降级标注，不炸全局；好连接的会话照常返回。"""
    from clu.daemon.rpc import dispatcher

    class SelectivePool(FakePool):
        async def run(
            self, label: str, command: str, *, timeout: float | None = None
        ) -> ExecResult:
            if label == "dead":
                from clu.ssh.connection import CluConnectionError

                raise CluConnectionError("Host is unreachable")
            if command.startswith("tmux list-sessions"):
                return ExecResult(command, 0, "clu/good/train|1700000000|0\n", "", 1)
            return ExecResult(command, 0, "", "", 1)

    store = ConfigStore(tmp_path / "connections.toml")
    store.add_connection(ConnectionConfig(label="good", kind="cluster", host="h", user="u"))
    store.add_connection(ConnectionConfig(label="dead", kind="local", host="h", user="u"))
    ledger = Ledger(tmp_path / "l.sqlite3")
    await ledger.open()
    ctx = DaemonContext(
        store=store, pool=SelectivePool(), ledger=ledger, socket_path=tmp_path / "s.sock"
    )
    result = await dispatcher.dispatch(ctx, "session.list", {})
    assert [s["purpose"] for s in result["sessions"]] == ["train"]
    assert [u["connection"] for u in result["unreachable"]] == ["dead"]
