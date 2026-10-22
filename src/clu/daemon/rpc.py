"""RPC 分发：unix socket 请求路由到各服务。M1 方法：ping / server.* / connection.*。"""

import asyncio
import logging
import os
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

import clu.session_backend as session_backend
import clu.slurm.alloc as allocops
import clu.slurm.commands as slurm
import clu.tmux as tmux
from clu import __version__
from clu.config.store import ConfigStore, ConnectionConfig
from clu.ledger.db import Ledger
from clu.ledger.query import LedgerFilter
from clu.policy import check_exec, check_submit_script
from clu.profiles import Profile
from clu.protocol import (
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_LOOP_DETECTED,
    ERR_METHOD_NOT_FOUND,
    ERR_NOT_FOUND,
    CluError,
)
from clu.slurm.alloc import probe_session
from clu.ssh.connection import CluConnectionError, ExecResult
from clu.ssh.forward import ForwardError, ForwardManager
from clu.ssh.pool import ConnectionPool
from clu.transfer import sftp

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
    forwards: ForwardManager = field(default_factory=ForwardManager)


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
        except (sftp.TransferError, ForwardError) as exc:
            raise RpcFailure("transfer_failed", str(exc)) from exc
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
        command,
        kind=cfg.kind,
        extra_heavy=profile.heavy_patterns if profile else None,
        heavy_guard=profile.heavy_guard if profile else True,
    )
    if not decision.allowed:
        raise RpcFailure("policy_denied", f"{decision.message}；建议：{decision.suggestion}")
    recent = await ctx.ledger.count_recent_identical(label, command)
    if recent >= 5:  # 循环熔断：相同命令 2 分钟内执行 5 次即拒绝（v0.2）
        raise RpcFailure(
            ERR_LOOP_DETECTED,
            f"相同命令 120s 内已执行 {recent} 次，疑似 agent 循环。"
            "请立即停止重试：此前每次调用的完整结果都在 tool_result 里，直接汇总回答即可。",
        )
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


@dispatcher.method("slurm.resources")
async def _slurm_resources(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """节点级 + 分区级 CPU/GPU 余量；partition / filter（cpu_free|cpu_gpu_free|
    fully_idle）/ gpu_only / include_nodes 从一开始就能限定查看范围。"""
    label = str(params["label"])
    _require_cluster(ctx, label)
    return await slurm.resources(
        _runner(ctx, label),
        partition=params.get("partition"),
        flt=params.get("filter"),
        gpu_only=bool(params.get("gpu_only")),
        include_nodes=bool(params.get("include_nodes")),
    )


@dispatcher.method("slurm.submit")
async def _slurm_submit(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """sbatch 提交：脚本/日志统一落 ~/.clu/jobs/<label>/，作业入库 jobs 台账。"""
    label = str(params["label"])
    script = str(params["script"])
    _require_cluster(ctx, label)
    decision = check_submit_script(script)
    if not decision.allowed:
        raise RpcFailure("policy_denied", decision.message)
    result = await slurm.submit(
        _runner(ctx, label),
        script,
        label=label,
        name=params.get("name"),
        partition=params.get("partition"),
        nodes=int(params["nodes"]) if params.get("nodes") else None,
        cpus=int(params["cpus"]) if params.get("cpus") else None,
        gpus=int(params["gpus"]) if params.get("gpus") else None,
        time_limit=params.get("time_limit"),
    )
    await ctx.ledger.upsert_job(
        connection=label,
        job_id=str(result["job_id"]),
        state="PENDING",
        name=result.get("name"),
        script_path=result.get("script_path"),
        script=str(result.get("script") or ""),
        partition=result.get("partition"),
        requested_nodes=str(result["nodes"]) if result.get("nodes") else None,
        cpus=result.get("cpus"),
        gpus=result.get("gpus"),
        stdout_path=result.get("stdout_path"),
        stderr_path=result.get("stderr_path"),
    )
    await ctx.ledger.record(
        kind="submit",
        connection=label,
        command=script[:500],
        job_id=str(result["job_id"]),
        summary=f"提交作业 {result['job_id']}",
    )
    return {"connection": label, **result}


@dispatcher.method("job.log")
async def _job_log(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """读台账作业的 stdout/stderr（路径在提交时已落库，不依赖 scontrol）。"""
    label = str(params["label"])
    job_id = str(params["job_id"])
    tail = int(params.get("tail") or 100)
    row = await ctx.ledger.get_job(label, job_id)
    if row is None:
        raise RpcFailure(ERR_NOT_FOUND, f"台账中无作业 {label}/{job_id}")
    out, err = row.get("stdout_path"), row.get("stderr_path")
    result: dict[str, Any] = {
        "job_id": job_id,
        "state": row.get("state"),
        "stdout_path": out,
        "stderr_path": err,
    }
    if out:
        got = await ctx.pool.run(
            label, f"tail -n {tail} {shlex.quote(str(out))} 2>/dev/null || true"
        )
        result["stdout"] = got.stdout
    else:
        result["stdout"] = ""
    if err:
        got = await ctx.pool.run(
            label, f"tail -n {tail} {shlex.quote(str(err))} 2>/dev/null || true"
        )
        result["stderr"] = got.stdout
    else:
        result["stderr"] = ""
    return result


@dispatcher.method("job.refresh")
async def _job_refresh(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """立即核对台账中非终态作业（daemon 平时每 JOB_CHECK_INTERVAL 自动核对）。"""
    return await check_tracked_jobs(ctx)


@dispatcher.method("job.list")
async def _job_list(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """作业台账：本地库记录的提交历史与最近一次核对的状态。"""
    rows = await ctx.ledger.list_jobs(
        params.get("label"), active_only=bool(params.get("active_only"))
    )
    return {"jobs": rows}


async def check_tracked_jobs(ctx: "DaemonContext") -> dict[str, int]:
    """巡检一轮台账内的非终态作业，刷新状态与节点。"""
    active = await ctx.ledger.list_jobs(active_only=True)
    by_conn: dict[str, list[dict[str, object]]] = {}
    for row in active:
        by_conn.setdefault(str(row["connection"]), []).append(row)
    updated = 0
    for label, rows in by_conn.items():
        ids = [str(r["job_id"]) for r in rows]
        try:
            found = await asyncio.wait_for(slurm.check_jobs(_runner(ctx, label), ids), timeout=30.0)
        except Exception:
            continue
        for row in rows:
            info = found.get(str(row["job_id"]))
            if info is None:
                continue
            await ctx.ledger.update_job(
                label,
                str(row["job_id"]),
                state=info["state"],
                node_list=info.get("node_list"),
                exit_code=info.get("exit_code"),
            )
            updated += 1
    return {"checked": len(active), "updated": updated}


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
    """抢占接口：默认 GPU 优先、无则 CPU；resource=gpu/cpu 指定类型；
    partition/gpus/cpus 可显式指定。直接执行（DESIGN §6 语义）。"""
    label = str(params["label"])
    _cfg, profile = _require_cluster(ctx, label)
    purpose = str(params.get("purpose") or "alloc")
    info = await allocops.allocate(
        _runner(ctx, label),
        label,
        purpose=purpose,
        default_cpus=profile.alloc_default_cpus if profile else None,
        resource=params.get("resource"),
        gpus=params.get("gpus"),
        cpus=params.get("cpus"),
        partition=params.get("partition"),
        wait=float(params.get("wait") or 120.0),
    )
    await ctx.ledger.record(
        kind="alloc",
        connection=label,
        command=str(info.get("request") or "alloc"),
        job_id=str(info.get("job_id") or "") or None,
        summary=f"alloc {purpose}: {info.get('state')} partition={info.get('partition')}",
    )
    if info.get("state") in ("granted", "pending"):  # 会话登记：daemon 重启后零探测恢复
        await ctx.ledger.upsert_session(
            label=label,
            purpose=purpose,
            job_id=info.get("job_id"),
            node=info.get("node"),
            partition=info.get("partition"),
            gpus=info.get("gpus"),
            cpus=info.get("cpus"),
            state=str(info.get("state")),
        )
    return info


@dispatcher.method("session.list")
async def _session_list(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """托管会话列表——登记表驱动：只探测有登记会话的连接（每连接一次 tmux 列举），
    逐会话校验存活（tmux 已亡自动销账）。deep=True 才全量扫描所有连接（收编库外会话）。
    不可达连接降级为上次已知状态（stale），不炸全局。"""
    deep = bool(params.get("deep"))
    registry = await ctx.ledger.list_sessions()
    data = ctx.store.load()

    if deep:
        labels = [str(name) for name in data.connections]
    else:
        labels = []
        for row in registry:
            lbl = str(row["label"])
            if lbl in data.connections and lbl not in labels:
                labels.append(lbl)

    async def probe(one: str) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        try:
            rows = await asyncio.wait_for(tmux.list_sessions(_runner(ctx, one), one), timeout=6.0)
            return rows, None
        except Exception as exc:
            return [], {"connection": one, "error": f"{type(exc).__name__}: {exc}"[:160]}

    pairs = await asyncio.gather(*(probe(one) for one in labels))
    live = {(str(r["connection"]), str(r["purpose"])): r for rows, _ in pairs for r in rows}
    unreachable = [err for _, err in pairs if err is not None]
    dead_conns = {str(e["connection"]) for e in unreachable}

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for row in registry:
        key = (str(row["label"]), str(row["purpose"]))
        if key[0] not in data.connections:  # 连接已删除：销账
            await ctx.ledger.delete_session(*key)
            continue
        merged[key] = dict(row, connection=row["label"])
        non_tmux = row.get("backend") in ("setsid", "pty-daemon")
        if key[0] in dead_conns:
            merged[key]["verified"] = False  # 不可达：上次已知（stale）
        elif key in live or non_tmux:  # 非 tmux 后端：登记表即真相（info 时探活）
            merged[key]["verified"] = True
        else:
            await ctx.ledger.delete_session(*key)  # tmux 已亡：销账
            merged.pop(key)
    for key, row in live.items():
        if key not in merged:
            merged[key] = dict(row, verified=True, state="granted", source="tmux")
            await ctx.ledger.upsert_session(  # 收编库外会话（deep 扫描或库丢失遗留）
                label=key[0], purpose=key[1], state="granted"
            )
    sessions = sorted(merged.values(), key=lambda r: str(r.get("updated_ts") or ""))
    return {"sessions": sessions, "unreachable": unreachable, "checked": labels}


async def check_registered_sessions(ctx: "DaemonContext") -> dict[str, int]:
    """巡检一轮登记表：逐会话探测刷新状态；消亡自动销账并记留痕。"""
    registry = await ctx.ledger.list_sessions()
    data = ctx.store.load()
    updated = kept = 0
    removed: list[str] = []
    for row in registry:
        label, purpose = str(row["label"]), str(row["purpose"])
        if label not in data.connections:
            await ctx.ledger.delete_session(label, purpose)
            removed.append(f"{label}/{purpose}")
            continue
        try:
            info = await asyncio.wait_for(
                probe_session(_runner(ctx, label), label, purpose), timeout=6.0
            )
        except Exception:
            kept += 1
            continue
        state = str(info.get("state"))
        if state == "failed":
            await ctx.ledger.delete_session(label, purpose)
            await ctx.ledger.record(
                kind="release",
                connection=label,
                command=f"watchdog {purpose}",
                summary=f"会话消亡自动销账：{purpose}",
            )
            removed.append(f"{label}/{purpose}")
        else:
            await ctx.ledger.upsert_session(
                label=label,
                purpose=purpose,
                state=state,
                job_id=info.get("job_id"),
                node=info.get("node"),
            )
            updated += 1
    return {
        "checked": len(registry),
        "updated": updated,
        "removed": len(removed),
        "unreachable": kept,
    }


async def _sftp_put(ctx: DaemonContext, label: str, local: Path, remote: str) -> None:
    """经连接池 SFTP 通道上传（ptyd 二进制部署用）。"""
    from clu.transfer import sftp as sftp_mod

    conn = await ctx.pool.acquire(label)
    real = sftp_mod._real(conn)
    async with real.raw.start_sftp_client() as sftp:
        await sftp.put(str(local), remote)


@dispatcher.method("session.start")
async def _session_start(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """在任意连接（集群/服务器）上启动托管会话（保活）。

    后端自动选择：远端有 tmux → tmux 交互会话（可 attach）；
    无 tmux → 部署 pty-daemon 静态守护（可 attach，远端零依赖）；
    部署不可行且显式给出 command → setsid 后台保活（不可 attach，仅日志）。
    """
    label = str(params["label"])
    purpose = str(params["purpose"])
    command = params.get("command")
    _get_connection(ctx, label)
    runner = _runner(ctx, label)
    auto_mode = "backend" not in params
    backend = params.get("backend") or (
        "tmux" if await session_backend.has_tmux(runner) else "pty-daemon"
    )

    ptyd_bin: str | None = None
    if backend == "pty-daemon":
        import clu.ptyd as ptyd_mod

        def _put(local: Path, remote: str) -> Awaitable[None]:
            return _sftp_put(ctx, label, local, remote)

        try:
            ptyd_bin = await ptyd_mod.ensure_binary(runner, _put)
            await ptyd_mod.ensure_running(runner, ptyd_bin)
        except (CluError, CluConnectionError) as exc:
            if command and auto_mode:
                backend = "setsid"  # 自动模式：回退 setsid
            else:
                raise RpcFailure(
                    ERR_NOT_FOUND,
                    f"远端无 tmux，pty-daemon 部署失败：{exc.message}；"
                    "可提供 command 走 setsid 保活，或在远端安装 tmux",
                ) from exc
    if backend == "setsid" and not command:
        raise RpcFailure(ERR_INVALID_PARAMS, "setsid 后台保活必须提供 command")

    info = await session_backend.start(
        runner,
        label,
        purpose,
        backend=backend,
        command=command if backend == "setsid" else None,
        ptyd_bin=ptyd_bin,
    )
    if info.get("state") == "failed":
        return {"connection": label, **info}
    await ctx.ledger.upsert_session(
        label=label,
        purpose=purpose,
        backend=backend,
        pid=info.get("pid"),
        state=str(info.get("state") or "running"),
    )
    await ctx.ledger.record(
        kind="session",
        connection=label,
        command=command or backend,
        summary=f"会话 {purpose}: {backend}/running",
    )
    return {"connection": label, **info}


async def _remote_ptyd_bin(runner: Any) -> str:
    """远端 ptyd 二进制绝对路径（部署过才有；info/release 用）。"""
    home = (await runner('printf "%s" "$HOME"')).stdout.strip()
    return f"{home}/.clu/bin/clu-ptyd"


@dispatcher.method("session.info")
async def _session_info(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    purpose = str(params["purpose"])
    _get_connection(ctx, label)
    runner = _runner(ctx, label)
    row = next(
        (
            r
            for r in await ctx.ledger.list_sessions()
            if r["label"] == label and r["purpose"] == purpose
        ),
        None,
    )
    backend = str((row or {}).get("backend") or "tmux")

    if backend == "setsid" and row and row.get("pid"):
        from clu.session_backend import info_setsid

        info = await info_setsid(runner, int(str(row.get("pid") or 0)), None)
        await ctx.ledger.upsert_session(label=label, purpose=purpose, state=str(info.get("state")))
        return dict(row, **info)
    if backend == "pty-daemon":
        from clu.session_backend import info_ptyd

        ptyd_bin = await _remote_ptyd_bin(runner)
        info = await info_ptyd(runner, ptyd_bin, purpose)
        await ctx.ledger.upsert_session(label=label, purpose=purpose, state=str(info.get("state")))
        return dict(row or {}, backend=backend, purpose=purpose, **info)

    info = await allocops.session_info(runner, label, purpose)
    if info.get("state") == "granted":  # 顺带刷新登记表
        await ctx.ledger.upsert_session(
            label=label,
            purpose=purpose,
            job_id=info.get("job_id"),
            node=info.get("node"),
            state="granted",
        )
    return info


@dispatcher.method("session.release")
async def _session_release(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    label = str(params["label"])
    purpose = str(params["purpose"])
    _get_connection(ctx, label)
    runner = _runner(ctx, label)
    row = next(
        (
            r
            for r in await ctx.ledger.list_sessions()
            if r["label"] == label and r["purpose"] == purpose
        ),
        None,
    )
    backend = str((row or {}).get("backend") or "tmux")

    released = False
    if backend == "setsid":
        from clu.session_backend import release_setsid

        if row and row.get("pid"):
            released = await release_setsid(runner, int(str(row.get("pid") or 0)))
    elif backend == "pty-daemon":
        from clu.session_backend import release_ptyd

        ptyd_bin = await _remote_ptyd_bin(runner)
        released = await release_ptyd(runner, ptyd_bin, purpose)
    else:
        released = bool((await allocops.release(runner, label, purpose))["released"])
    await ctx.ledger.delete_session(label, purpose)
    await ctx.ledger.record(
        kind="release",
        connection=label,
        command=f"release {purpose}",
        summary=f"释放 {backend} 会话 {purpose}: {'成功' if released else '不存在'}",
    )
    return {"connection": label, "purpose": purpose, "released": released, "backend": backend}


@dispatcher.method("transfer.upload")
async def _transfer_upload(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """上传本地文件到远端（SFTP，走连接池连接）。"""
    label = str(params["label"])
    _get_connection(ctx, label)
    conn = await ctx.pool.acquire(label)
    result = await sftp.upload(conn, str(params["local_path"]), str(params["remote_path"]))
    await ctx.ledger.record(
        kind="upload",
        connection=label,
        command=f"{result['local_path']} -> {result['remote_path']}",
        summary=f"上传 {result['bytes']} 字节",
    )
    return dict(result)


@dispatcher.method("transfer.download")
async def _transfer_download(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """下载远端文件到本地（SFTP）。"""
    label = str(params["label"])
    _get_connection(ctx, label)
    conn = await ctx.pool.acquire(label)
    result = await sftp.download(conn, str(params["remote_path"]), str(params["local_path"]))
    await ctx.ledger.record(
        kind="download",
        connection=label,
        command=f"{result['remote_path']} -> {result['local_path']}",
        summary=f"下载 {result['bytes']} 字节",
    )
    return dict(result)


@dispatcher.method("forward.add")
async def _forward_add(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    """建立本地转发：127.0.0.1:<local_port|随机> -> 远端 host:port（经连接池）。"""
    label = str(params["label"])
    _get_connection(ctx, label)
    row = await ctx.forwards.add(
        label,
        str(params["remote_host"]),
        int(params["remote_port"]),
        local_port=int(params.get("local_port") or 0),
    )
    await ctx.ledger.record(
        kind="forward",
        connection=label,
        command=f"-L {row['local_port']}:{row['remote_host']}:{row['remote_port']}",
        summary=f"转发 {row['id']}",
    )
    return dict(row)


@dispatcher.method("forward.list")
async def _forward_list(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"forwards": ctx.forwards.list()}


@dispatcher.method("forward.remove")
async def _forward_remove(ctx: DaemonContext, params: dict[str, Any]) -> dict[str, Any]:
    removed = ctx.forwards.remove(str(params["id"]))
    if not removed:
        raise RpcFailure(ERR_NOT_FOUND, f"未知转发：{params['id']}")
    return {"id": params["id"], "removed": True}
