"""SSH 连接池：按 label 懒建长连，断线后台自动重连（指数退避，DESIGN §5.1）。"""

import asyncio
from collections.abc import Callable

from clu.config.store import ConnectionConfig
from clu.ssh.connection import (
    CluConnectionError,
    Connector,
    ConnState,
    ExecResult,
    ManagedConnection,
    SSHConn,
)


class ConnectionPool:
    """daemon 持有的连接注册表；config_loader 每次调用都重新读取配置。"""

    def __init__(
        self,
        config_loader: Callable[[], dict[str, ConnectionConfig]],
        connector: Connector,
        *,
        backoff_start: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        self._loader = config_loader
        self._connector = connector
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._conns: dict[str, ManagedConnection] = {}
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    # ---- 内部 ----
    def _configs(self) -> dict[str, ConnectionConfig]:
        return self._loader()

    def _managed(self, label: str, cfg: ConnectionConfig) -> ManagedConnection:
        existing = self._conns.get(label)
        if existing is None or existing.config != cfg:
            if existing is not None:
                existing.mark_lost("配置变更")
            existing = ManagedConnection(
                cfg,
                self._connector,
                backoff_start=self._backoff_start,
                backoff_max=self._backoff_max,
            )
            self._conns[label] = existing
        return existing

    def _schedule_reconnect(self, label: str) -> None:
        if self._closed or label in self._reconnect_tasks:
            return
        self._reconnect_tasks[label] = asyncio.create_task(
            self._reconnect_loop(label), name=f"clu-reconnect-{label}"
        )

    async def _reconnect_loop(self, label: str) -> None:
        try:
            while not self._closed:
                managed = self._conns.get(label)
                if managed is None:
                    return
                await asyncio.sleep(managed.wait_seconds())
                if self._closed or managed.state is ConnState.ONLINE:
                    return
                cfg = managed.config
                try:
                    tunnel = await self.acquire(cfg.proxy) if cfg.proxy else None
                    await managed.ensure_online(tunnel)
                    return
                except CluConnectionError:
                    continue
                except asyncio.CancelledError:
                    raise
        finally:
            self._reconnect_tasks.pop(label, None)

    # ---- 对外 ----
    async def acquire(self, label: str) -> SSHConn:
        configs = self._configs()
        cfg = configs.get(label)
        if cfg is None:
            raise CluConnectionError(f"未知连接：{label}")
        tunnel = await self.acquire(cfg.proxy) if cfg.proxy else None
        managed = self._managed(label, cfg)
        try:
            return await managed.ensure_online(tunnel)
        except CluConnectionError:
            self._schedule_reconnect(label)
            raise

    async def run(self, label: str, command: str, *, timeout: float | None = None) -> ExecResult:
        configs = self._configs()
        cfg = configs.get(label)
        if cfg is None:
            raise CluConnectionError(f"未知连接：{label}")
        tunnel = await self.acquire(cfg.proxy) if cfg.proxy else None
        managed = self._managed(label, cfg)
        try:
            result = await managed.run(command, timeout=timeout, tunnel=tunnel)
        except CluConnectionError:
            self._schedule_reconnect(label)
            raise
        task = self._reconnect_tasks.pop(label, None)
        if task is not None and not task.done():
            task.cancel()
        return result

    async def drop(self, label: str) -> None:
        """主动断开并触发后台重连（测试/运维用）。"""
        managed = self._conns.get(label)
        if managed is None:
            return
        managed.mark_lost("dropped")
        self._schedule_reconnect(label)

    def status(self) -> list[dict[str, object]]:
        """全部已知连接的健康快照（含从未连接的 idle）。"""
        rows: list[dict[str, object]] = []
        for label, cfg in sorted(self._configs().items()):
            managed = self._conns.get(label)
            rows.append(
                {
                    "label": label,
                    "kind": cfg.kind,
                    "host": cfg.host,
                    "port": cfg.port,
                    "user": cfg.user,
                    "proxy": cfg.proxy,
                    "profile": cfg.profile,
                    "state": managed.state.value if managed else ConnState.IDLE.value,
                    "last_error": managed.last_error if managed else None,
                    "last_online_ts": managed.last_online_ts if managed else None,
                }
            )
        return rows

    async def close(self) -> None:
        self._closed = True
        for task in self._reconnect_tasks.values():
            task.cancel()
        self._reconnect_tasks.clear()
        for managed in self._conns.values():
            managed.mark_lost("pool closed")
