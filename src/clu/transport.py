"""RPC 传输层（M1，v0.2.0 跨平台）：POSIX unix socket / Windows 回环 TCP + token。

Windows 的 asyncio 不支持 unix socket；named pipe 需 pywin32 且与 asyncio
集成复杂。127.0.0.1 回环 TCP + 启动时生成的一次性 token（写 daemon.port）
是等价安全的替代：仅本机可达，token 未泄露时无认证面。
两种传输共用同一 NDJSON 协议，客户端代码无感知。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import clu.constants as C
from clu.protocol import CluError

IS_WINDOWS = sys.platform == "win32"  # 单点开关，测试可 monkeypatch

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def _port_file() -> Path:
    return C.daemon_port_file()


async def connect(timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """建立到 daemon 的连接；Windows 上完成 token 握手。"""
    if not IS_WINDOWS:
        unix_connect = getattr(asyncio, "open_unix_connection")  # noqa: B009  typeshed: 仅 POSIX
        try:
            return await asyncio.wait_for(unix_connect(str(C.daemon_socket())), timeout)
        except (TimeoutError, OSError) as exc:
            raise CluError("daemon_unreachable", f"无法连接 daemon：{exc}") from exc
    try:
        info = json.loads(_port_file().read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
    except (OSError, ValueError, KeyError) as exc:
        raise CluError("daemon_unreachable", f"无法读取 daemon 端口文件：{exc}") from exc
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    except (TimeoutError, OSError) as exc:
        raise CluError("daemon_unreachable", f"无法连接 daemon：{exc}") from exc
    writer.write((json.dumps({"token": token}) + "\n").encode("utf-8"))
    await writer.drain()
    return reader, writer


async def serve(handle_client: Handler) -> tuple[asyncio.AbstractServer, str | None]:
    """启动 RPC 监听。返回 (server, auth_token)；auth_token 仅 Windows 非 None，
    服务端在首行校验。"""
    if not IS_WINDOWS:
        path = C.daemon_socket()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()  # 调用方保证无存活 daemon 后才拉起
        unix_serve = getattr(asyncio, "start_unix_server")  # noqa: B009  typeshed: 仅 POSIX
        server = await unix_serve(handle_client, path=str(path))
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(0o600)
        return server, None
    token = secrets.token_hex(32)
    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    pf = _port_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
    return server, token


def cleanup() -> None:
    """停止后清理运行时残留（socket 文件 / 端口文件）。"""
    if not IS_WINDOWS:
        with contextlib.suppress(OSError):
            C.daemon_socket().unlink(missing_ok=True)
    else:
        with contextlib.suppress(OSError):
            _port_file().unlink(missing_ok=True)
