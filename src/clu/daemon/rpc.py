"""RPC 分发：unix socket 请求路由到各服务。M1 方法：ping / server.* / connection.*。"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clu import __version__
from clu.config.store import ConfigStore
from clu.ledger.db import Ledger
from clu.protocol import ERR_INTERNAL, ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND
from clu.ssh.connection import CluConnectionError
from clu.ssh.pool import ConnectionPool

log = logging.getLogger("clu.daemon.rpc")

Handler = Callable[["DaemonContext", dict[str, Any]], Awaitable[dict[str, Any]]]


class RpcFailure(Exception):
    """handler 内主动失败（携带错误码）。"""

    def __init__(self, code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass
class DaemonContext:
    """daemon 运行态：各服务与生命周期事件。"""

    store: ConfigStore
    pool: ConnectionPool
    ledger: Ledger
    socket_path: Path
    started: float = field(default_factory=time.time)
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)


class Dispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self._handlers[name] = fn
            return fn

        return register

    async def dispatch(
        self, ctx: DaemonContext, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcFailure(ERR_METHOD_NOT_FOUND, f"未知方法：{method}")
        try:
            return await handler(ctx, params)
        except RpcFailure:
            raise
        except CluConnectionError as exc:
            raise RpcFailure("connection_failed", exc.message, exc.retry_after) from exc
        except KeyError as exc:
            raise RpcFailure(ERR_INVALID_PARAMS, f"缺少参数：{exc}") from exc
        except ValidationError as exc:
            raise RpcFailure(ERR_INVALID_PARAMS, str(exc)) from exc
        except Exception as exc:
            log.exception("处理 %s 时出错", method)
            raise RpcFailure(ERR_INTERNAL, f"{type(exc).__name__}: {exc}") from exc


dispatcher = Dispatcher()


@dispatcher.method("ping")
async def _ping(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "version": __version__}


@dispatcher.method("server.status")
async def _server_status(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": __version__,
        "pid": os.getpid(),
        "uptime_s": round(time.time() - ctx.started, 1),
        "socket": str(ctx.socket_path),
        "connections": ctx.pool.status(),
    }


@dispatcher.method("server.shutdown")
async def _server_shutdown(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    ctx.shutdown.set()
    return {"stopping": True}


@dispatcher.method("connection.list")
async def _connection_list(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"connections": ctx.pool.status()}


@dispatcher.method("connection.check")
async def _connection_check(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    result = await ctx.pool.run(label, "true", timeout=15.0)
    await ctx.ledger.record(
        kind="exec",
        connection=label,
        command="true",
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        summary="connection.check 探测",
    )
    row = next((r for r in ctx.pool.status() if r["label"] == label), {})
    return {
        "label": label,
        "state": row.get("state"),
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
    }
