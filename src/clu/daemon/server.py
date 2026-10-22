"""daemon 服务器：unix socket 监听、NDJSON 会话、信号优雅退出（DESIGN §3）。"""

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import clu.constants as C
from clu import transport
from clu.client import CluError
from clu.config.store import ConfigStore
from clu.daemon.rpc import DaemonContext, Dispatcher, RpcFailure
from clu.daemon.rpc import dispatcher as default_dispatcher
from clu.ledger.db import Ledger
from clu.protocol import (
    RpcErr,
    RpcError,
    RpcOk,
    RpcRequest,
    decode_request,
    encode_line,
)
from clu.ssh.connection import KnownHosts, make_asyncssh_connector
from clu.ssh.pool import ConnectionPool

log = logging.getLogger("clu.daemon")


async def _respond(writer: asyncio.StreamWriter, resp: RpcOk | RpcErr) -> None:
    writer.write(encode_line(resp))
    await writer.drain()


async def handle_client(
    ctx: DaemonContext,
    disp: Dispatcher,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    auth_token: str | None = None,
) -> None:
    """单个客户端会话：逐行处理请求直到 EOF。"""
    peer = writer.get_extra_info("peername")
    log.debug("客户端接入：%s", peer)
    try:
        if auth_token is not None:
            auth_line = await reader.readline()
            ok = False
            with contextlib.suppress(ValueError):
                ok = json.loads(auth_line).get("token") == auth_token
            if not ok:
                log.warning("客户端认证失败：%s", peer)
                writer.close()
                return
        while True:
            line = await reader.readline()
            if not line:
                break
            req: RpcRequest | None = None
            started = 0.0
            try:
                req = decode_request(line)
                started = time.monotonic()
                result = await disp.dispatch(ctx, req.method, req.params)
                log.info(
                    "rpc %s label=%s ok %.0fms",
                    req.method,
                    req.params.get("label", "-"),
                    (time.monotonic() - started) * 1000,
                )
                response: RpcOk | RpcErr = RpcOk(id=req.id, result=result)
            except RpcFailure as exc:
                if req:
                    log.info(
                        "rpc %s label=%s err %s %.0fms",
                        req.method,
                        req.params.get("label", "-"),
                        exc.code,
                        (time.monotonic() - started) * 1000,
                    )
                response = RpcErr(
                    id=req.id if req else "",
                    error=RpcError(code=exc.code, message=exc.message, retry_after=exc.retry_after),
                )
            except Exception as exc:
                log.exception("请求处理崩溃")
                response = RpcErr(
                    id=req.id if req else "",
                    error=RpcError(code="internal", message=f"{type(exc).__name__}: {exc}"),
                )
            await _respond(writer, response)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        log.debug("客户端断开：%s", peer)


async def _session_watch(ctx: "DaemonContext") -> None:
    """定期巡检：会话登记（状态刷新/消亡销账）与作业台账（状态核对）。"""
    from clu.daemon.rpc import check_registered_sessions, check_tracked_jobs

    session_interval = C.SESSION_CHECK_INTERVAL
    job_interval = C.JOB_CHECK_INTERVAL
    next_session = next_job = 0.0
    while True:
        now = time.monotonic()
        if now >= next_session:
            next_session = now + session_interval
            try:
                report = await check_registered_sessions(ctx)
                if report["checked"]:
                    log.info(
                        "会话巡检：checked=%s updated=%s removed=%s unreachable=%s",
                        report["checked"],
                        report["updated"],
                        report["removed"],
                        report["unreachable"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("会话巡检出错")
        if now >= next_job:
            next_job = now + job_interval
            try:
                report = await check_tracked_jobs(ctx)
                if report["checked"]:
                    log.info(
                        "作业巡检：checked=%s updated=%s",
                        report["checked"],
                        report["updated"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("作业巡检出错")
        await asyncio.sleep(min(max(next_session, next_job) - time.monotonic(), 30.0))


async def run_daemon(
    *,
    socket_path: Path | None = None,
    connections_path: Path | None = None,
    ledger_path: Path | None = None,
    known_hosts_path: Path | None = None,
    dispatcher: Dispatcher | None = None,
) -> None:
    """daemon 主入口（`python -m clu.daemon`）。"""
    socket = socket_path or C.daemon_socket()
    log_path = C.daemon_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    store = ConfigStore(connections_path or C.connections_path())
    known_hosts = KnownHosts(known_hosts_path or C.known_hosts_path())
    pool = ConnectionPool(
        lambda: store.load().connections,
        make_asyncssh_connector(known_hosts),
    )
    ledger = Ledger(ledger_path or C.ledger_path())
    try:
        await ledger.open()
    except Exception as exc:
        log.warning("留痕库打开失败（继续运行，不记录）：%s", exc)

    ctx = DaemonContext(store=store, pool=pool, ledger=ledger, socket_path=socket)
    ctx.forwards.bind(pool.acquire)
    disp = dispatcher or default_dispatcher

    async def _client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await handle_client(ctx, disp, r, w, auth_token=auth_token)

    server, auth_token = await transport.serve(_client)
    log.info("daemon 就绪 pid=%s socket=%s", os.getpid(), socket)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows Proactor 无信号处理
            loop.add_signal_handler(sig, ctx.shutdown.set)
    watcher = asyncio.create_task(_session_watch(ctx))
    try:
        await ctx.shutdown.wait()
    finally:
        watcher.cancel()
        log.info("daemon 退出")
        server.close()
        await server.wait_closed()
        ctx.forwards.close_all()
        await pool.close()
        await ledger.close()
        transport.cleanup()


def daemon_alive(socket_path: Path | None = None, timeout: float = 1.5) -> bool:
    """同步探测 daemon 是否存活（ping）。可在事件循环内调用（自动卸载到线程）。"""
    from clu.client import CluClient

    path = socket_path or C.daemon_socket()

    async def _probe() -> bool:
        try:
            async with CluClient(path, timeout=timeout) as client:
                result = await client.request("ping")
                return bool(result.get("pong"))
        except CluError:
            return False

    def _run() -> bool:
        return asyncio.run(_probe())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_run).result()


def ensure_daemon(
    *, socket_path: Path | None = None, wait_timeout: float = C.ENSURE_WAIT_TIMEOUT
) -> dict[str, Any]:
    """不在运行则拉起（detached 子进程），等待就绪。"""
    import subprocess
    import time

    path = socket_path or C.daemon_socket()
    if daemon_alive(path):
        return {"started": False, "alive": True, "socket": str(path)}
    if not transport.IS_WINDOWS and path.exists():
        path.unlink()
    log_file = C.daemon_log()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as fh:
        # 跨平台分离式启动：POSIX setsid / Windows DETACHED_PROCESS
        spawn_kwargs: dict[str, Any] = {"start_new_session": True}
        if sys.platform == "win32":
            spawn_kwargs = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            }
        proc = subprocess.Popen(
            [sys.executable, "-m", "clu.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=fh,
            close_fds=True,
            **spawn_kwargs,
        )
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if daemon_alive(path):
            return {"started": True, "alive": True, "pid": proc.pid, "socket": str(path)}
        if proc.poll() is not None:
            raise CluError(
                "internal", f"daemon 启动即退出（code={proc.returncode}），日志：{log_file}"
            )
        time.sleep(0.1)
    proc.terminate()
    raise CluError("internal", f"daemon {wait_timeout}s 内未就绪，日志：{log_file}")
