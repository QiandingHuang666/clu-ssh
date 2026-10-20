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

import clu.slurm.alloc as allocops
import clu.slurm.commands as slurm
import clu.tmux as tmux
from clu import __version__
from clu.config.store import ConfigStore, ConnectionConfig
from clu.ledger.db import Ledger
from clu.ledger.query import LedgerFilter
from clu.policy import check_exec, check_submit_script
from clu.profiles import Profile
from clu.protocol import ERR_INTERNAL, ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, ERR_NOT_FOUND
from clu.ssh.connection import CluConnectionError, ExecResult
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


_MAX_OUTPUT_CHARS = 65536


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[clu] 输出超长，截断（原 {len(text)} 字符）"


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


def _get_connection(ctx: DaemonContext, label: str) -> tuple[ConnectionConfig, Profile | None]:
    data = ctx.store.load()
    cfg = data.connections.get(label)
    if cfg is None:
        raise RpcFailure(ERR_NOT_FOUND, f"未知连接：{label}")
    profile = data.profiles.get(cfg.profile) if cfg.profile else None
    return cfg, profile


def _runner(ctx: DaemonContext, label: str) -> slurm.Runner:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        return await ctx.pool.run(label, command, timeout=timeout)

    return run


@dispatcher.method("exec")
async def _exec(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """执行单条远程命令（安全门 + 留痕）。"""
    label = str(params["label"])
    command = str(params["command"])
    timeout = float(params.get("timeout") or 60.0)
    cfg, profile = _get_connection(ctx, label)
    decision = check_exec(
        command, kind=cfg.kind, extra_heavy=profile.heavy_patterns if profile else None
    )
    if not decision.allowed:
        raise RpcFailure("policy_denied", f"{decision.message}；建议：{decision.suggestion}")
    result = await ctx.pool.run(label, command, timeout=timeout)
    await ctx.ledger.record(
        kind="exec",
        connection=label,
        command=command,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
    )
    return {
        "connection": label,
        "command": command,
        "exit_code": result.exit_code,
        "stdout": _truncate(result.stdout),
        "stderr": _truncate(result.stderr),
        "duration_ms": result.duration_ms,
    }


def _require_cluster(ctx: DaemonContext, label: str) -> tuple[ConnectionConfig, Profile | None]:
    cfg, profile = _get_connection(ctx, label)
    if cfg.kind != "cluster":
        raise RpcFailure(
            ERR_INVALID_PARAMS, f"连接 {label} 的 kind={cfg.kind}，slurm 工具仅用于 cluster 连接"
        )
    return cfg, profile


@dispatcher.method("slurm.jobs")
async def _slurm_jobs(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    cfg, _ = _require_cluster(ctx, label)
    states = [str(s) for s in params["states"]] if params.get("states") else None
    jobs = await slurm.list_jobs(_runner(ctx, label), cfg.user, states)
    return {"connection": label, "jobs": jobs}


@dispatcher.method("slurm.status")
async def _slurm_status(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    _require_cluster(ctx, label)
    return await slurm.cluster_status(_runner(ctx, label))


@dispatcher.method("slurm.submit")
async def _slurm_submit(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    script = str(params["script"])
    _require_cluster(ctx, label)
    decision = check_submit_script(script)
    if not decision.allowed:
        raise RpcFailure("policy_denied", decision.message)
    result = await slurm.submit(_runner(ctx, label), script)
    await ctx.ledger.record(
        kind="submit",
        connection=label,
        command=script[:500],
        job_id=str(result["job_id"]),
        summary=f"提交作业 {result['job_id']}",
    )
    return {"connection": label, **result}


@dispatcher.method("slurm.log")
async def _slurm_log(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    job_id = str(params["job_id"])
    tail = int(params.get("tail") or 100)
    _require_cluster(ctx, label)
    log = await slurm.job_log(_runner(ctx, label), job_id, tail=tail)
    log["text"] = _truncate(str(log["text"]))
    return log


@dispatcher.method("slurm.cancel")
async def _slurm_cancel(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    job_id = str(params["job_id"])
    _require_cluster(ctx, label)
    result = await slurm.cancel(_runner(ctx, label), job_id)
    await ctx.ledger.record(
        kind="cancel",
        connection=label,
        command=f"scancel {job_id}",
        exit_code=int(str(result["exit_code"] or 0)),
        summary=f"取消作业 {job_id}",
    )
    return result


@dispatcher.method("ledger.query")
async def _ledger_query(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    records = await ctx.ledger.query(
        LedgerFilter(
            since=params.get("since"),
            until=params.get("until"),
            connection=params.get("connection"),
            kind=params.get("kind"),
            keyword=params.get("keyword"),
            job_id=params.get("job_id"),
            limit=int(params.get("limit") or 100),
        )
    )
    return {"records": records}


@dispatcher.method("slurm.alloc")
async def _slurm_alloc(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """申请交互式资源：tmux 内直接执行 salloc 并轮询到授予（DESIGN §6 语义）。"""
    label = str(params["label"])
    _cfg, profile = _require_cluster(ctx, label)
    purpose = str(params.get("purpose") or "alloc")
    info = await allocops.allocate(
        _runner(ctx, label),
        label,
        purpose=purpose,
        partition=params.get("partition"),
        gpus=params.get("gpus"),
        preempt=bool(params.get("preempt")),
        profile=profile,
        wait=float(params.get("wait") or 120.0),
    )
    await ctx.ledger.record(
        kind="alloc",
        connection=label,
        command=f"salloc purpose={purpose} partition={params.get('partition') or '-'}"
        f" gpus={params.get('gpus') or '-'} preempt={params.get('preempt') or False}",
        job_id=str(info.get("job_id") or "") or None,
        summary=f"alloc {purpose}: {info.get('state')}",
    )
    return info


@dispatcher.method("session.list")
async def _session_list(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = params.get("label")
    data = ctx.store.load()
    labels = [str(label)] if label else list(data.connections)
    sessions: list[dict[str, object]] = []
    for one in labels:
        if one not in data.connections:
            raise RpcFailure(ERR_NOT_FOUND, f"未知连接：{one}")
        sessions.extend(await tmux.list_sessions(_runner(ctx, one), one))
    return {"sessions": sessions}


@dispatcher.method("session.info")
async def _session_info(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    purpose = str(params["purpose"])
    _get_connection(ctx, label)
    return await allocops.session_info(_runner(ctx, label), label, purpose)


@dispatcher.method("session.release")
async def _session_release(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    purpose = str(params["purpose"])
    _get_connection(ctx, label)
    result = await allocops.release(_runner(ctx, label), label, purpose)
    await ctx.ledger.record(
        kind="release",
        connection=label,
        command=f"release {purpose}",
        summary=f"释放 alloc 会话 {purpose}: {'成功' if result['released'] else '不存在'}",
    )
    return result
