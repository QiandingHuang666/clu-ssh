"""连接池状态机测试（注入 fake 连接器，无真实 SSH）。"""

import asyncio
import contextlib

import pytest

from clu.config.store import ConnectionConfig
from clu.ssh.connection import CluConnectionError, ConnState, ExecResult
from clu.ssh.pool import ConnectionPool


class FakeConn:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self.runs = 0

    async def run(self, command: str, *, timeout: float | None = None) -> ExecResult:
        self.runs += 1
        return ExecResult(command=command, exit_code=0, stdout="ok", stderr="", duration_ms=1)

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self) -> None:
        self.conns: dict[str, FakeConn] = {}
        self.tunnels: dict[str, object] = {}
        self.calls = 0
        self.fail_labels: set[str] = set()

    async def __call__(self, cfg: ConnectionConfig, tunnel: object) -> FakeConn:
        self.calls += 1
        if cfg.label in self.fail_labels:
            raise OSError("connection refused")
        self.tunnels[cfg.label] = tunnel
        conn = FakeConn(cfg.label)
        self.conns[cfg.label] = conn
        return conn


def make_cfg(label: str, **kw: object) -> ConnectionConfig:
    return ConnectionConfig(label=label, host="h", user="u", **kw)  # type: ignore[arg-type]


def make_pool(configs: dict[str, ConnectionConfig], connector: FakeConnector) -> ConnectionPool:
    return ConnectionPool(lambda: configs, connector, backoff_start=0.02, backoff_max=0.08)


async def test_acquire_creates_once_and_reuses() -> None:
    connector = FakeConnector()
    pool = make_pool({"a": make_cfg("a")}, connector)
    c1 = await pool.acquire("a")
    c2 = await pool.acquire("a")
    assert c1 is c2
    assert connector.calls == 1
    row = next(r for r in pool.status() if r["label"] == "a")
    assert row["state"] == ConnState.ONLINE.value


async def test_unknown_label() -> None:
    pool = make_pool({}, FakeConnector())
    with pytest.raises(CluConnectionError, match="未知连接"):
        await pool.acquire("nope")


async def test_failure_enters_backoff_and_gates_retry() -> None:
    connector = FakeConnector()
    connector.fail_labels.add("a")
    pool = make_pool({"a": make_cfg("a")}, connector)
    with pytest.raises(CluConnectionError):
        await pool.acquire("a")
    calls_after_first = connector.calls
    with pytest.raises(CluConnectionError) as excinfo:
        await pool.acquire("a")  # 退避期内：不再调连接器
    assert excinfo.value.retry_after is not None
    assert connector.calls == calls_after_first
    await asyncio.sleep(0.03)
    with pytest.raises(CluConnectionError):
        await pool.acquire("a")  # 退避过后允许再试
    assert connector.calls == calls_after_first + 1


async def test_tunnel_composition() -> None:
    connector = FakeConnector()
    configs = {"jump": make_cfg("jump"), "t": make_cfg("t", proxy="jump")}
    pool = make_pool(configs, connector)
    await pool.acquire("t")
    assert connector.tunnels["t"] is connector.conns["jump"]


async def test_run_recovers_after_drop() -> None:
    connector = FakeConnector()
    pool = make_pool({"a": make_cfg("a")}, connector)
    r1 = await pool.run("a", "echo 1")
    assert r1.exit_code == 0
    await pool.drop("a")
    r2 = await pool.run("a", "echo 2")
    assert r2.exit_code == 0
    assert r2.duration_ms >= 0
    await pool.close()


async def test_background_reconnect_after_failure() -> None:
    connector = FakeConnector()
    connector.fail_labels.add("a")
    pool = make_pool({"a": make_cfg("a")}, connector)
    with pytest.raises(CluConnectionError):
        await pool.run("a", "true")
    connector.fail_labels.clear()  # 网络恢复
    for _ in range(200):  # 最多等 2s
        state = next(r["state"] for r in pool.status() if r["label"] == "a")
        if state == ConnState.ONLINE.value:
            break
        await asyncio.sleep(0.01)
    assert state == ConnState.ONLINE.value
    await pool.close()


async def test_connector_omits_client_keys_without_identity(monkeypatch) -> None:
    """identity 缺省时不得把 client_keys=None 传进 options（asyncssh 会当作无密钥）。"""
    import asyncssh

    from clu.ssh.connection import KnownHosts, make_asyncssh_connector

    captured: dict[str, object] = {}

    class FakeOptions:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    async def fake_connect(*args: object, **kw: object) -> object:
        raise OSError("stop-here")

    monkeypatch.setattr(asyncssh, "SSHClientConnectionOptions", FakeOptions)
    monkeypatch.setattr(asyncssh, "connect", fake_connect)

    connector = make_asyncssh_connector(KnownHosts(None))
    cfg = ConnectionConfig(label="x", host="h", user="u")  # 无 identity
    with contextlib.suppress(OSError):
        await connector(cfg, None)
    assert "client_keys" not in captured


def test_known_hosts_id_distinguishes_ports() -> None:
    """同 IP 不同端口是不同主机（frp 转发场景），known_hosts 条目必须区分。"""
    from clu.ssh.connection import known_hosts_id

    a = ConnectionConfig(label="a", host="120.27.1.1", port=22, user="u")
    b = ConnectionConfig(label="b", host="120.27.1.1", port=21563, user="u")
    assert known_hosts_id(a) == "120.27.1.1"
    assert known_hosts_id(b) == "[120.27.1.1]:21563"
    assert known_hosts_id(a) != known_hosts_id(b)
