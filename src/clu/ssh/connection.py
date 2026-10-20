"""单连接封装：状态机 + 退避重连 + asyncssh 真实连接器（含单级跳板与 accept-new host key）。

连接器以 Callable 注入：单测用 fake，daemon 用 make_asyncssh_connector 产物。
ManagedConnection 状态机：OFFLINE -> CONNECTING -> ONLINE；失败进 BACKOFF（指数退避）。
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import asyncssh

import clu.constants as C
from clu.config.store import ConnectionConfig


class ConnState(StrEnum):
    IDLE = "idle"  # 尚未尝试连接
    CONNECTING = "connecting"
    ONLINE = "online"
    BACKOFF = "backoff"  # 最近一次失败，退避等待中


@dataclass(frozen=True)
class ExecResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int


class SSHConn(Protocol):
    """daemon 依赖的最小连接协议。"""

    async def run(self, command: str, *, timeout: float | None = None) -> ExecResult: ...

    def close(self) -> None: ...


class CluConnectionError(Exception):
    """连接不可用；retry_after 为退避剩余秒数。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


Connector = Callable[[ConnectionConfig, "SSHConn | None"], Awaitable[SSHConn]]


class ManagedConnection:
    """单条连接的生命周期管理。"""

    def __init__(
        self,
        cfg: ConnectionConfig,
        connector: Connector,
        *,
        backoff_start: float = C.RECONNECT_BACKOFF_START,
        backoff_max: float = C.RECONNECT_BACKOFF_MAX,
    ) -> None:
        self._cfg = cfg
        self._connector = connector
        self._lock = asyncio.Lock()
        self._state = ConnState.IDLE
        self._conn: SSHConn | None = None
        self._last_error: str | None = None
        self._last_online_ts: float | None = None
        self._next_attempt_at = 0.0
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._backoff = backoff_start

    @property
    def config(self) -> ConnectionConfig:
        return self._cfg

    @property
    def state(self) -> ConnState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_online_ts(self) -> float | None:
        return self._last_online_ts

    def wait_seconds(self) -> float:
        """退避剩余时间（0 表示可以立即重试）。"""
        return max(0.0, self._next_attempt_at - time.monotonic())

    async def ensure_online(self, tunnel: SSHConn | None = None) -> SSHConn:
        """确保 ONLINE 并返回底层连接；退避期内快速失败。"""
        async with self._lock:
            if self._state is ConnState.ONLINE and self._conn is not None:
                return self._conn
            wait = self.wait_seconds()
            if wait > 0:
                raise CluConnectionError(
                    f"连接 {self._cfg.label} 处于退避期（{wait:.1f}s 后重试）", retry_after=wait
                )
            self._state = ConnState.CONNECTING
            try:
                conn = await self._connector(self._cfg, tunnel)
            except asyncio.CancelledError:
                self._state = ConnState.IDLE
                raise
            except Exception as exc:
                delay = self._backoff
                self._backoff = min(self._backoff * 2, self._backoff_max)
                self._next_attempt_at = time.monotonic() + delay
                self._state = ConnState.BACKOFF
                self._last_error = str(exc)
                raise CluConnectionError(
                    f"连接 {self._cfg.label} 失败：{exc}", retry_after=delay
                ) from exc
            self._conn = conn
            self._state = ConnState.ONLINE
            self._last_error = None
            self._last_online_ts = time.time()
            self._backoff = self._backoff_start
            self._next_attempt_at = 0.0
            return conn

    def mark_lost(self, reason: str) -> None:
        """连接断开时调用；关闭底层连接并复位为可立即重试。"""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
        self._conn = None
        if self._state is not ConnState.BACKOFF:
            self._state = ConnState.IDLE
        self._last_error = reason

    async def run(
        self, command: str, *, timeout: float | None = None, tunnel: SSHConn | None = None
    ) -> ExecResult:
        conn = await self.ensure_online(tunnel)
        started = time.monotonic()
        try:
            result = await conn.run(command, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.mark_lost(f"run 失败：{exc}")
            raise CluConnectionError(f"连接 {self._cfg.label} 执行失败：{exc}") from exc
        return replace(result, duration_ms=int((time.monotonic() - started) * 1000))


# ---- asyncssh 真实实现 ----


class KnownHosts:
    """accept-new 语义：首次见到主机 key 即记录；已存在则交给 asyncssh 严格校验。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path | None:
        return self._path

    def _entries(self) -> list[list[str]]:
        if self._path is None or not self._path.exists():
            return []
        return [line.split() for line in self._path.read_text().splitlines() if line.strip()]

    def has(self, host: str) -> bool:
        return any(entry and entry[0] == host for entry in self._entries())

    def key_line(self, host: str) -> str | None:
        for entry in self._entries():
            if entry and entry[0] == host:
                return " ".join(entry[1:])
        return None

    def add(self, host: str, line: str) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(f"{host} {line}\n")


def _key_line(key: asyncssh.SSHKey) -> str:
    """主机公钥的 OpenSSH 单行形式（known_hosts 条目去掉主机名字段）。"""
    return key.export_public_key().decode().strip()


class RealSSHConn:
    """asyncssh 连接的最小适配，满足 SSHConn 协议。"""

    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn

    @property
    def raw(self) -> asyncssh.SSHClientConnection:
        return self._conn

    async def run(self, command: str, *, timeout: float | None = None) -> ExecResult:
        if timeout is not None:
            result = await asyncio.wait_for(self._conn.run(command, check=False), timeout)
        else:
            result = await self._conn.run(command, check=False)
        return ExecResult(
            command=command,
            exit_code=result.exit_status,
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            duration_ms=0,
        )

    def close(self) -> None:
        self._conn.close()


def _resolve_identity(identity: str | None) -> Path | None:
    """IdentityFile 相对路径按 ~/.ssh/ 解析（OpenSSH 语义）。"""
    if identity is None:
        return None
    path = Path(identity).expanduser()
    if not path.is_absolute():
        path = Path.home() / ".ssh" / path
    return path


def known_hosts_id(cfg: ConnectionConfig) -> str:
    """known_hosts 条目名（OpenSSH 语义）：22 端口记裸 host，其余记 [host]:port。

    同 IP 不同端口可能是完全不同的主机（如 frp 转发到集群登录节点），必须区分。
    """
    return cfg.host if cfg.port == 22 else f"[{cfg.host}]:{cfg.port}"


def make_asyncssh_connector(known_hosts: KnownHosts) -> Connector:
    """构造真实连接器：心跳、单级跳板、accept-new host key。"""

    async def connect(cfg: ConnectionConfig, tunnel: SSHConn | None) -> SSHConn:
        # accept-new：已知主机严格校验（交给 asyncssh），未知主机首次接受并记录
        kh_id = known_hosts_id(cfg)
        known = known_hosts.has(kh_id) and known_hosts.path is not None
        identity = _resolve_identity(cfg.identity)
        # 注意：client_keys 不能显式传 None——asyncssh 会当作"无密钥"而非"用默认"
        options_kwargs: dict[str, object] = {}
        if identity is not None:
            options_kwargs["client_keys"] = [str(identity)]
        options = asyncssh.SSHClientConnectionOptions(
            keepalive_interval=C.HEARTBEAT_INTERVAL,
            keepalive_count_max=C.HEARTBEAT_COUNT_MAX,
            connect_timeout=C.DEFAULT_CONNECT_TIMEOUT,
            known_hosts=str(known_hosts.path) if known else None,
            **options_kwargs,
        )
        tunnel_arg = tunnel.raw if isinstance(tunnel, RealSSHConn) else None
        conn = await asyncssh.connect(
            cfg.host, port=cfg.port, username=cfg.user, options=options, tunnel=tunnel_arg
        )
        if not known:  # accept-new：首次连接后记录主机 key
            server_key = conn.get_server_host_key()
            if server_key is not None:
                known_hosts.add(kh_id, _key_line(server_key))
        return RealSSHConn(conn)

    return connect
