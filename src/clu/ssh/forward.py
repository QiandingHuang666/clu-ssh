"""本地端口转发（-L 最小集，DESIGN §5.3）：经连接池连接建立，daemon 内存态。

最小集限制：转发绑定在连接对象上，连接断开后转发失效，不自动重建
（重连重建进路线图）；daemon 重启后转发清空，需重新 add。
"""

import contextlib
from collections.abc import Awaitable, Callable

import asyncssh

from clu.ssh.connection import CluConnectionError, RealSSHConn, SSHConn


class ForwardError(Exception):
    """转发建立/管理失败。"""


def _unavailable(label: str) -> SSHConn:
    raise CluConnectionError(f"连接不可用：{label}")


class ForwardManager:
    """管理本机 TCP 监听 -> 远端 host:port 的转发列表。"""

    def __init__(self) -> None:
        self._provider: Callable[[str], Awaitable[SSHConn]] | None = None
        self._forwards: dict[str, dict[str, object]] = {}
        self._seq = 0

    def bind(self, provider: Callable[[str], Awaitable[SSHConn]]) -> None:
        """注入连接提供者（daemon 启动时绑 pool.acquire）。"""
        self._provider = provider

    async def add(
        self, label: str, remote_host: str, remote_port: int, local_port: int = 0
    ) -> dict[str, object]:
        if self._provider is None:
            raise ForwardError("转发管理未绑定连接池")
        conn = await self._provider(label)
        if not isinstance(conn, RealSSHConn):
            raise ForwardError("转发需要真实 SSH 连接")
        try:
            listener: object = await conn.raw.forward_local_port(
                "127.0.0.1", local_port, remote_host, remote_port
            )
        except (asyncssh.Error, OSError) as exc:
            raise ForwardError(f"转发建立失败：{exc}") from exc
        actual_port = local_port
        with contextlib.suppress(Exception):
            actual_port = int(listener.get_port() or local_port)  # type: ignore[attr-defined]
        self._seq += 1
        fid = f"fwd-{self._seq}"
        self._forwards[fid] = {
            "id": fid,
            "connection": label,
            "local_host": "127.0.0.1",
            "local_port": actual_port,
            "remote_host": remote_host,
            "remote_port": remote_port,
            "listener": listener,
        }
        return self._row(fid)

    def _row(self, fid: str) -> dict[str, object]:
        f = self._forwards[fid]
        return {k: v for k, v in f.items() if k != "listener"}

    def list(self) -> list[dict[str, object]]:
        return [self._row(fid) for fid in self._forwards]

    def remove(self, fid: str) -> bool:
        f = self._forwards.pop(fid, None)
        if f is None:
            return False
        listener = f["listener"]
        with contextlib.suppress(Exception):
            listener.close()  # type: ignore[attr-defined]
        return True

    def close_all(self) -> None:
        for fid in list(self._forwards):
            self.remove(fid)
