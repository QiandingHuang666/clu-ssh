"""daemon 生命周期：ensure 拉起 / stop / status（拉起实现在 server.py，此处重导出）。"""

from pathlib import Path
from typing import Any

import clu.constants as C
from clu.client import CluClient
from clu.daemon.server import daemon_alive, ensure_daemon

__all__ = ["daemon_alive", "daemon_status", "ensure_daemon", "stop_daemon"]


async def stop_daemon(socket_path: Path | None = None) -> dict[str, Any]:
    """请求 daemon 优雅退出。"""
    path = socket_path or C.daemon_socket()
    async with CluClient(path) as client:
        return dict(await client.request("server.shutdown"))


async def daemon_status(socket_path: Path | None = None) -> dict[str, Any]:
    """读取 server.status。"""
    path = socket_path or C.daemon_socket()
    async with CluClient(path) as client:
        return dict(await client.request("server.status"))
