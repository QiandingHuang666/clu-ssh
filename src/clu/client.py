"""unix socket 薄客户端库：CLI 与 MCP server 共用。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import clu.constants as C
from clu.protocol import RpcErr, RpcRequest, decode_response, encode_line


class CluError(Exception):
    """daemon 返回的结构化错误，或本地通信失败。"""

    def __init__(self, code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class CluClient:
    """单连接复用的 RPC 客户端；断线自动重连一次。"""

    def __init__(
        self,
        socket_path: Path | None = None,
        timeout: float = C.CLIENT_TIMEOUT,
    ) -> None:
        self._socket_path = socket_path or C.daemon_socket()
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> CluClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

    async def _connect(self, retry: bool = True) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)), self._timeout
            )
        except (TimeoutError, OSError) as exc:
            raise CluError("daemon_unreachable", f"无法连接 daemon：{exc}") from exc
        if retry is False:
            return

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起一次 RPC。连接失效时自动重连一次重发。"""
        for attempt in (1, 2):
            await self._connect()
            assert self._writer is not None and self._reader is not None
            req = RpcRequest(method=method, params=params or {})
            try:
                self._writer.write(encode_line(req))
                await self._writer.drain()
                line = await asyncio.wait_for(self._reader.readline(), self._timeout)
            except (TimeoutError, OSError) as exc:
                await self.aclose()
                if attempt == 1:
                    continue
                raise CluError("daemon_unreachable", f"daemon 通信失败：{exc}") from exc
            if not line:
                await self.aclose()
                if attempt == 1:
                    continue
                raise CluError("daemon_unreachable", "daemon 关闭了连接") from None
            resp = decode_response(line)
            if isinstance(resp, RpcErr):
                raise CluError(resp.error.code, resp.error.message, resp.error.retry_after)
            return resp.result
        raise CluError("internal", "unreachable")  # pragma: no cover
