"""clu 命令行入口：daemon 的薄客户端；连接配置管理为本地文件操作。"""

import asyncio
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from clu import AUTHOR_EMAIL, AUTHOR_NAME, __version__
from clu.client import CluClient, CluError
from clu.config.ssh_config import import_into_store
from clu.config.store import ConfigError, ConfigStore
from clu.constants import connections_path
from clu.daemon.lifecycle import daemon_status, ensure_daemon, stop_daemon

app = typer.Typer(
    name="clu",
    help="SSH 会话保持/管理 + Slurm 集群与服务器 agent 能力层。",
    no_args_is_help=True,
    invoke_without_command=True,
)
connection_app = typer.Typer(help="连接配置管理。", no_args_is_help=True)
server_app = typer.Typer(help="daemon 生命周期管理。", no_args_is_help=True)
session_app = typer.Typer(help="tmux 托管会话。", no_args_is_help=True)
forward_app = typer.Typer(help="本地端口转发（-L 最小集）。", no_args_is_help=True)
app.add_typer(connection_app, name="connection")
app.add_typer(server_app, name="server")
app.add_typer(session_app, name="session")
app.add_typer(forward_app, name="forward")

console = Console()


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=code)


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="显示版本并退出。"),
) -> None:
    """clu：连接池与集群操作的统一入口。"""
    if version:
        typer.echo(f"clu {__version__}")
        typer.echo(f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>")
        raise typer.Exit()


# ---- server ----


@server_app.command("ensure")
def server_ensure() -> None:
    """确保 daemon 运行中；不在则自动拉起。"""
    try:
        result = ensure_daemon()
    except CluError as exc:
        _fail(f"daemon 拉起失败：{exc.message}（日志：~/.local/state 或运行时目录下 daemon.log）")
        return
    if result["started"]:
        console.print(f"[green]daemon 已拉起[/green]（pid={result.get('pid')}）")
    else:
        console.print("daemon 已在运行")


@server_app.command("status")
def server_status() -> None:
    """查看 daemon 与连接池状态。"""
    try:
        result = asyncio.run(daemon_status())
    except CluError as exc:
        _fail(f"daemon 未运行或不可达：{exc.message}（先执行 clu server ensure）")
        return
    console.print(
        f"daemon [green]在线[/green]  v{result.get('version')}  pid={result.get('pid')}"
        f"  uptime={result.get('uptime_s')}s"
    )
    connections = result.get("connections") or []
    if connections:
        table = Table(title="连接池")
        for col in ("label", "kind", "host", "state", "last_error"):
            table.add_column(col)
        for row in connections:
            state = str(row.get("state"))
            style = "green" if state == "online" else "yellow"
            table.add_row(
                str(row.get("label")),
                str(row.get("kind")),
                f"{row.get('host')}:{row.get('port')}",
                f"[{style}]{state}[/{style}]",
                str(row.get("last_error") or ""),
            )
        console.print(table)


@server_app.command("stop")
def server_stop() -> None:
    """停止 daemon。"""
    try:
        asyncio.run(stop_daemon())
        console.print("daemon 已停止")
    except CluError as exc:
        _fail(f"停止失败：{exc.message}")


# ---- connection ----


@connection_app.command("import")
def connection_import(
    config: Path = typer.Option(
        Path.home() / ".ssh" / "config", "--config", help="ssh config 源文件路径。"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="同名连接覆盖写入。"),
) -> None:
    """从 ssh config 一次性导入公钥连接（副本，解析 ProxyJump）。"""
    try:
        store = ConfigStore(connections_path())
        report = import_into_store(store, config, overwrite=overwrite)
    except (ConfigError, OSError) as exc:
        _fail(f"导入失败：{exc}")
        return
    if report.added:
        console.print(f"[green]导入 {len(report.added)} 条[/green]：{', '.join(report.added)}")
    if report.skipped:
        console.print(f"[yellow]跳过（已存在）[/yellow]：{', '.join(report.skipped)}")
    for warning in report.warnings:
        console.print(f"[yellow]注意[/yellow] {warning}")
    if not (report.added or report.skipped or report.warnings):
        console.print("没有可导入的条目")


@connection_app.command("list")
def connection_list() -> None:
    """列出已管理的连接及其池状态。"""
    try:
        data = ConfigStore(connections_path()).load()
    except ConfigError as exc:
        _fail(f"配置读取失败：{exc}")
        return
    if not data.connections:
        console.print("暂无连接（先执行 clu connection import 或手写 connections.toml）")
        return
    health: dict[str, str] = {}
    try:
        result = asyncio.run(daemon_status())
        for row in result.get("connections") or []:
            health[str(row.get("label"))] = str(row.get("state"))
    except CluError:
        health = {}
    table = Table(title=f"连接（daemon {'在线' if health else '未运行'}）")
    for col in ("label", "kind", "host", "user", "proxy", "profile", "state"):
        table.add_column(col)
    for label, cfg in sorted(data.connections.items()):
        state = health.get(label, "-")
        style = "green" if state == "online" else "yellow"
        table.add_row(
            label,
            cfg.kind,
            f"{cfg.host}:{cfg.port}",
            cfg.user,
            cfg.proxy or "-",
            cfg.profile or "-",
            f"[{style}]{state}[/{style}]",
        )
    console.print(table)


@connection_app.command("add")
def connection_add(
    label: str = typer.Argument(..., help="连接 label（字母数字开头，可用 ._-）。"),
    host: str = typer.Option(..., "--host", help="主机名或 IP。"),
    user: str = typer.Option(..., "--user", help="登录用户。"),
    port: int = typer.Option(22, "--port", help="端口。"),
    kind: str = typer.Option("instance", "--kind", help="cluster | instance | local。"),
    identity: str = typer.Option(
        None, "--identity", "-i", help="私钥路径（缺省走 ssh-agent/默认 key）。"
    ),
    proxy: str = typer.Option(None, "--proxy", help="单级跳板：另一连接的 label。"),
    profile: str = typer.Option(None, "--profile", help="定制 profile 名。"),
    overwrite: bool = typer.Option(False, "--overwrite", help="同名覆盖。"),
) -> None:
    """手动添加连接（Slurm 集群记得 --kind cluster）。"""
    from clu.config.store import ConnectionConfig

    if kind not in ("cluster", "instance", "local"):
        _fail(f"kind 必须是 cluster/instance/local，收到：{kind}")
        return
    try:
        store = ConfigStore(connections_path())
        written = store.add_connection(
            ConnectionConfig(
                label=label,
                kind=kind,
                host=host,
                port=port,
                user=user,
                identity=identity,
                proxy=proxy,
                profile=profile,
            ),
            overwrite=overwrite,
        )
    except (ConfigError, ValueError) as exc:
        _fail(f"添加失败：{exc}")
        return
    if written:
        console.print(f"[green]已添加[/green] {label}（{kind} {host}:{port}）")
    else:
        console.print(f"[yellow]已存在[/yellow] {label}（--overwrite 覆盖）")


@connection_app.command("remove")
def connection_remove(label: str) -> None:
    """删除连接条目。"""
    try:
        removed = ConfigStore(connections_path()).remove_connection(label)
    except ConfigError as exc:
        _fail(f"删除失败：{exc}")
        return
    if removed:
        console.print(f"已删除 {label}")
    else:
        _fail(f"不存在：{label}")


@connection_app.command("check")
def connection_check(label: str) -> None:
    """探测一条连接：经 daemon 连接池建立 SSH 并执行 true。"""

    async def _check() -> dict[str, object]:
        async with CluClient() as client:
            return await client.request("connection.check", {"label": label})

    try:
        ensure_daemon()
        result = asyncio.run(_check())
    except CluError as exc:
        _fail(
            f"[red]探测失败[/red] {label}: {exc.message}"
            + (f"（{exc.retry_after:.0f}s 后可重试）" if exc.retry_after else "")
        )
        return
    console.print(
        f"[green]在线[/green] {label}: exit={result.get('exit_code')}"
        f" 用时 {result.get('duration_ms')}ms"
    )


@app.command("exec")
def exec_command(
    label: str = typer.Argument(..., help="连接 label。"),
    command: list[str] = typer.Argument(..., help="远程命令（按空格拼接执行）。"),
    timeout: float = typer.Option(60.0, "--timeout", help="超时秒数。"),
) -> None:
    """在连接上执行一条远程命令（受安全门约束）。"""

    async def _do() -> dict[str, object]:
        async with CluClient(timeout=max(timeout + 30, 60)) as client:
            return await client.request(
                "exec",
                {"label": label, "command": " ".join(command), "timeout": timeout},
            )

    try:
        ensure_daemon()
        result = asyncio.run(_do())
    except CluError as exc:
        _fail(f"执行失败：{exc.message}")
        return
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        console.print(stdout.rstrip())
    if stderr:
        console.print(f"[red]{stderr.rstrip()}[/red]")
    console.print(f"[dim]exit={result.get('exit_code')}  {result.get('duration_ms')}ms[/dim]")
    raise typer.Exit(code=int(str(result.get("exit_code") or 0)))


def _do_request(method: str, params: dict[str, object]) -> dict[str, object]:
    async def _call() -> dict[str, object]:
        async with CluClient(timeout=180.0) as client:
            return dict(await client.request(method, params))

    result = asyncio.run(_call())
    return result


@session_app.command("start")
def session_start(
    label: str,
    purpose: str,
    backend: str = typer.Option(
        None,
        "--backend",
        help="tmux | pty-daemon | setsid；缺省自动（tmux → pty-daemon → setsid）。",
    ),
    command: str = typer.Option(
        None, "--command", "-c", help="setsid 后端必填的后台命令；其他后端忽略。"
    ),
) -> None:
    """在任意连接（集群/服务器）上开一个保活托管会话。

    后端自动选择：远端有 tmux → 交互会话（clu attach 可回）；
    无 tmux → 部署 pty-daemon 静态守护（可 attach，零远端依赖）；
    部署不可行且给了 --command → setsid 后台保活（不可 attach，仅日志）。
    """
    try:
        ensure_daemon()
        result = _do_request(
            "session.start",
            {"label": label, "purpose": purpose, "backend": backend, "command": command},
        )
    except CluError as exc:
        _fail(f"会话启动失败：{exc.message}")
        return
    if result.get("state") == "failed":
        _fail(f"会话启动失败：{result.get('error')}")
        return
    backend_used = str(result.get("backend"))
    console.print(f"[green]会话已启动[/green] {label}/{purpose}（backend={backend_used}）")
    if backend_used == "tmux":
        console.print(f"接入：[bold]clu attach {label} {purpose}[/bold]（exit 即释放）")
    elif backend_used == "pty-daemon":
        console.print("接入：让 Claude Code 调 session_info 后 attach，或直接 ssh 上去用 clu-ptyd")
    else:
        console.print(
            f"后台保活（不可交互）；查看日志：session_info（或远端 ~/.clu/sessions/{purpose}.log）"
        )


@session_app.command("list")
def session_list(
    label: str = typer.Argument(None, help="连接 label；缺省按登记表检查全部会话。"),
    deep: bool = typer.Option(
        False, "--deep", help="全量扫描所有连接的 tmux（收编登记表外的会话）。"
    ),
) -> None:
    """列出托管会话（tmux / pty-daemon / setsid，登记表驱动，含存活校验与自动销账）。"""
    try:
        ensure_daemon()
        result = _do_request("session.list", {"label": label, "deep": deep})
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return
    raw_sessions = result.get("sessions")
    sessions = (
        [dict(r) for r in raw_sessions if isinstance(r, dict)]
        if isinstance(raw_sessions, list)
        else []
    )
    raw_unreachable = result.get("unreachable")
    unreachable = (
        [dict(r) for r in raw_unreachable if isinstance(r, dict)]
        if isinstance(raw_unreachable, list)
        else []
    )
    if sessions:
        table = Table(title="托管会话")
        for col in ("connection", "purpose", "state", "attached"):
            table.add_column(col)
        for row in sessions:
            table.add_row(
                str(row.get("connection")),
                str(row.get("purpose")),
                "在线",
                str(row.get("attached")),
            )
        console.print(table)
    else:
        console.print("暂无托管会话" + ("（部分连接不可达，见下）" if unreachable else ""))
    for row in unreachable:
        console.print(f"[yellow]连接不可达[/yellow] {row.get('connection')}：{row.get('error')}")


@app.command("resources")
def resources_cmd(
    label: str = typer.Argument(..., help="cluster 连接 label。"),
    partition: str = typer.Option(None, "--partition", "-p", help="只看该分区。"),
    cpu_free: bool = typer.Option(False, "--cpu-free", help="只看有空闲 CPU 的节点。"),
    cpu_gpu_free: bool = typer.Option(
        False, "--cpu-gpu-free", help="只看 CPU 和 GPU 都有空闲的节点。"
    ),
    fully_idle: bool = typer.Option(False, "--fully-idle", help="只看 CPU 与 GPU 全空闲的节点。"),
    gpu_only: bool = typer.Option(
        False, "--gpu-only", help="只看有 GPU 的节点（可与其他条件叠加）。"
    ),
    nodes: bool = typer.Option(False, "--nodes", help="输出节点明细表。"),
) -> None:
    """集群资源余量：每分区/每节点的 CPU 与 GPU（总量/已占/空闲）。"""
    flt = (
        "cpu_free"
        if cpu_free
        else "cpu_gpu_free"
        if cpu_gpu_free
        else "fully_idle"
        if fully_idle
        else None
    )
    if sum(bool(x) for x in (cpu_free, cpu_gpu_free, fully_idle)) > 1:
        _fail("--cpu-free/--cpu-gpu-free/--fully-idle 只能选一个")
        return
    try:
        ensure_daemon()
        result = _do_request(
            "slurm.resources",
            {
                "label": label,
                "partition": partition,
                "filter": flt,
                "gpu_only": gpu_only,
                "include_nodes": nodes,
            },
        )
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return

    parts = result.get("partitions")
    filters_desc = str(result.get("filters") or "")
    table = Table(title=f"{label} 资源余量  [dim]{filters_desc}[/dim]")
    for col in ("分区", "节点(空闲)", "CPU 空闲/总", "GPU 空闲/总"):
        table.add_column(col)
    for pname, p in parts.items() if isinstance(parts, dict) else []:
        gpus = "  ".join(f"{g}:{v['free']}/{v['total']}" for g, v in p["gpus"].items()) or "-"
        cpu = p["cpu"]
        table.add_row(
            str(pname),
            f"{p['nodes']}({p['idle_nodes']})",
            f"{cpu['free']}/{cpu['total']}",
            gpus,
        )
    console.print(table)

    raw = result.get("nodes")
    node_rows = [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if node_rows:
        ntable = Table(title=f"节点明细（{len(node_rows)}）")
        for col in ("节点", "状态", "CPU 空闲/总", "GPU 空闲/总", "分区"):
            ntable.add_column(col)
        for n in node_rows:
            gpus = (
                "  ".join(f"{g}:{v['free']}/{v['total']}" for g, v in n.get("gpus", {}).items())
                or "-"
            )
            cpu = n.get("cpu", {})
            state = str(n.get("state", ""))
            style = (
                "green" if "IDLE" in state else ("red" if not n.get("usable", True) else "yellow")
            )
            ntable.add_row(
                str(n.get("node")),
                f"[{style}]{state}[/{style}]",
                f"{cpu.get('free')}/{cpu.get('total')}",
                gpus,
                ",".join(n.get("partitions") or []),
            )
        console.print(ntable)
    console.print(f"[dim]{result.get('note')}[/dim]")


@app.command("alloc")
def alloc_cmd(
    label: str,
    purpose: str = typer.Option("alloc", "--purpose", help="会话用途名。"),
    gpu: bool = typer.Option(False, "--gpu", help="只抢 GPU。"),
    cpu: bool = typer.Option(False, "--cpu", help="只抢 CPU。"),
    gpus: int = typer.Option(1, "--gpus", help="GPU 卡数。"),
    cpus: int = typer.Option(None, "--cpus", help="CPU 核数（cpu 资源默认 4）。"),
    partition: str = typer.Option(None, "--partition", "-p", help="显式分区（跳过自动选择）。"),
    wait: float = typer.Option(120.0, "--wait", help="等待授予秒数。"),
) -> None:
    """抢占资源（默认 GPU 优先无则 CPU），granted 后 clu attach 接入计算节点。"""
    if gpu and cpu:
        _fail("--gpu 与 --cpu 只能选一个")
        return
    resource = "gpu" if gpu else "cpu" if cpu else None
    try:
        ensure_daemon()
        result = _do_request(
            "slurm.alloc",
            {
                "label": label,
                "purpose": purpose,
                "resource": resource,
                "gpus": gpus,
                "cpus": cpus,
                "partition": partition,
                "wait": wait,
            },
        )
    except CluError as exc:
        _fail(f"抢占失败：{exc.message}")
        return
    state = str(result.get("state"))
    if state == "exists":
        _fail(f"已在运行：{result.get('error')}")
        return
    style = "green" if state == "granted" else "yellow" if state == "pending" else "red"
    console.print(
        f"[{style}]{state}[/{style}]  job={result.get('job_id')}  node={result.get('node')}"
        f"  partition={result.get('partition')}（{result.get('chosen_by')}）"
    )
    if state == "granted":
        console.print(f"接入：[bold]clu attach {label} {purpose}[/bold]（exit 即释放）")
    if result.get("availability"):
        console.print(f"[dim]余量：{result.get('availability')}[/dim]")
    if result.get("error"):
        console.print(f"[red]{result.get('error')}[/red]")


@session_app.command("release")
def session_release(
    label: str,
    purpose: str = typer.Argument(None, help="会话用途名；与 --all 二选一。"),
    all_sessions: bool = typer.Option(False, "--all", help="释放该连接全部托管会话。"),
) -> None:
    """释放托管会话（按后端：tmux 杀会话 / pty-daemon 与 setsid 杀进程）。"""
    if not purpose and not all_sessions:
        _fail("给出 PURPOSE 或使用 --all")
        return
    if purpose and all_sessions:
        _fail("PURPOSE 与 --all 二选一")
        return
    try:
        ensure_daemon()
        if all_sessions:
            listed = _do_request("session.list", {"label": label})
            raw = listed.get("sessions")
            purposes = (
                [str(dict(r).get("purpose")) for r in raw if isinstance(r, dict)]
                if isinstance(raw, list)
                else []
            )
            if not purposes:
                console.print("该连接暂无托管会话")
                return
            for one in purposes:
                result = _do_request("session.release", {"label": label, "purpose": one})
                console.print(
                    f"已释放 {one}"
                    + ("" if result.get("released") else " [yellow]（不存在）[/yellow]")
                )
            return
        result = _do_request("session.release", {"label": label, "purpose": purpose})
    except CluError as exc:
        _fail(f"释放失败：{exc.message}")
        return
    if result.get("released"):
        console.print(f"[green]已释放[/green] {label}/{purpose}（资源已归还）")
    else:
        _fail(f"不存在：{label}/{purpose}")


@app.command("jobs")
def jobs_cmd(
    label: str = typer.Argument(..., help="cluster 连接 label。"),
    states: str = typer.Option(None, "--states", help="过滤状态，逗号分隔（如 RUNNING,PENDING）。"),
) -> None:
    """查看集群实时队列（当前用户作业，squeue 实时查询）。"""
    state_list = [s.strip().upper() for s in states.split(",")] if states else None
    try:
        ensure_daemon()
        result = _do_request("slurm.jobs", {"label": label, "states": state_list})
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return
    raw = result.get("jobs")
    rows = [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if not rows:
        console.print("当前没有匹配的作业")
        return
    table = Table(title=f"{label} 实时队列")
    for col in ("job_id", "状态", "名称", "分区", "已运行", "时限", "节点", "CPU", "GPU", "原因"):
        table.add_column(col)
    for r in rows:
        state = str(r.get("state") or "")
        style = "green" if state == "RUNNING" else ("yellow" if state in ("PENDING",) else "red")
        table.add_row(
            str(r.get("job_id")),
            f"[{style}]{state}[/{style}]",
            str(r.get("name") or "-"),
            str(r.get("partition") or "-"),
            str(r.get("elapsed") or "-"),
            str(r.get("time_limit") or "-"),
            str(r.get("nodes") or "-"),
            str(r.get("cpus") or "-"),
            str(r.get("gres") or "-"),
            str(r.get("reason") or "-"),
        )
    console.print(table)


@app.command("submit")
def submit_cmd(
    label: str,
    script_file: Path = typer.Option(None, "--script-file", "-f", help="sbatch 脚本文件。"),
    script: str = typer.Option(None, "--script", help="脚本内容（与 -f 二选一；缺省读 stdin）。"),
    name: str = typer.Option(None, "--name", "-J", help="作业名。"),
    partition: str = typer.Option(None, "--partition", "-p", help="分区。"),
    nodes: int = typer.Option(None, "--nodes", "-N", help="节点数。"),
    cpus: int = typer.Option(None, "--cpus", "-c", help="每任务 CPU 核数。"),
    gpus: int = typer.Option(None, "--gpus", help="GPU 卡数。"),
    time_limit: str = typer.Option(None, "--time", "-t", help="时限，如 4:00:00。"),
) -> None:
    """提交批处理作业：脚本与日志落到远端 ~/.clu/jobs/<连接>/，并记入本地台账。"""
    import sys

    if script_file is not None and script is not None:
        _fail("--script-file 与 --script 二选一")
        return
    if script_file is not None:
        try:
            content = script_file.read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"读取脚本失败：{exc}")
            return
    elif script is not None:
        content = script
    else:
        if sys.stdin.isatty():
            _fail("给出 --script-file / --script，或通过管道传入脚本")
            return
        content = sys.stdin.read()
    try:
        ensure_daemon()
        result = _do_request(
            "slurm.submit",
            {
                "label": label,
                "script": content,
                "name": name,
                "partition": partition,
                "nodes": nodes,
                "cpus": cpus,
                "gpus": gpus,
                "time_limit": time_limit,
            },
        )
    except CluError as exc:
        _fail(f"提交失败：{exc.message}")
        return
    console.print(f"[green]已提交[/green] job={result.get('job_id')}  name={result.get('name')}")
    console.print(f"脚本：{result.get('script_path')}")
    if result.get("stdout_path"):
        console.print(f"stdout：{result.get('stdout_path')}")
    if result.get("stderr_path"):
        console.print(f"stderr：{result.get('stderr_path')}")


job_app = typer.Typer(help="作业台账。", no_args_is_help=True)
app.add_typer(job_app, name="job")


@job_app.command("log")
def job_log_cmd(
    label: str,
    job_id: str,
    tail: int = typer.Option(100, "--tail", help="行数。"),
) -> None:
    """读取台账作业的 stdout/stderr（路径提交时已落库）。"""
    try:
        ensure_daemon()
        result = _do_request("job.log", {"label": label, "job_id": job_id, "tail": tail})
    except CluError as exc:
        _fail(f"读取失败：{exc.message}")
        return
    console.print(f"[dim]state={result.get('state')}  stdout={result.get('stdout_path')}[/dim]")
    if result.get("stdout"):
        console.print(str(result["stdout"]).rstrip())
    if result.get("stderr"):
        console.print("[red]--- stderr ---[/red]")
        console.print(str(result["stderr"]).rstrip())


@job_app.command("list")
def job_list_cmd(
    label: str = typer.Argument(None, help="连接 label；缺省全部。"),
    active_only: bool = typer.Option(False, "--active", help="只看非终态作业。"),
) -> None:
    """列出本地台账中的作业（daemon 定期核对其状态）。"""
    try:
        ensure_daemon()
        result = _do_request("job.list", {"label": label, "active_only": active_only})
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return
    raw = result.get("jobs")
    rows = [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if not rows:
        console.print("台账中暂无作业")
        return
    table = Table(title="作业台账")
    for col in ("连接", "job_id", "名称", "状态", "节点", "分区", "提交时间"):
        table.add_column(col)
    for r in rows:
        state = str(r.get("state") or "")
        style = (
            "green"
            if state in ("COMPLETED",)
            else (
                "red"
                if state
                in (
                    "FAILED",
                    "CANCELLED",
                    "TIMEOUT",
                    "OUT_OF_MEMORY",
                    "NODE_FAIL",
                    "PREEMPTED",
                    "REVOKED",
                )
                else "yellow"
            )
        )
        table.add_row(
            str(r.get("connection")),
            str(r.get("job_id")),
            str(r.get("name") or "-"),
            f"[{style}]{state}[/{style}]",
            str(r.get("node_list") or "-"),
            str(r.get("partition") or "-"),
            str(r.get("submitted_ts") or "-"),
        )
    console.print(table)


@app.command("attach")
def attach(label: str, purpose: str) -> None:
    """接入托管会话的 tmux（交互式，经系统 ssh -t）。"""
    import os

    from clu.config.store import ConfigError, ConfigStore
    from clu.tmux import session_name

    try:
        data = ConfigStore(connections_path()).load()
    except ConfigError as exc:
        _fail(f"配置读取失败：{exc}")
        return
    cfg = data.connections.get(label)
    if cfg is None:
        _fail(f"未知连接：{label}")
        return
    args = ["ssh", "-t", "-p", str(cfg.port)]
    if cfg.identity:
        args += ["-i", os.path.expanduser(cfg.identity)]
    if cfg.proxy:
        jump = data.connections.get(cfg.proxy)
        if jump is not None:
            jump_target = f"{jump.user}@{jump.host}"
            if jump.port != 22:
                jump_target += f":{jump.port}"
            args += ["-J", jump_target]
    target = f"{cfg.user}@{cfg.host}"
    args += [target, "tmux attach -t " + session_name(label, purpose)]
    console.print(f"[dim]{' '.join(args[: len(args) - 1])} tmux attach ...[/dim]")
    if sys.platform == "win32":
        # Windows 无 execvp 语义：前台运行 ssh，退出码透传
        raise typer.Exit(subprocess.run(args, check=False).returncode)
    os.execvp("ssh", args)


@forward_app.command("add")
def forward_add(
    label: str,
    remote_host: str,
    remote_port: int,
    local_port: int = typer.Option(0, "--local-port", "-l", help="本地端口，0 为随机分配。"),
) -> None:
    """建立转发：本机端口 -> 远端 host:port（经连接池隧道）。"""
    try:
        ensure_daemon()
        row = _do_request(
            "forward.add",
            {
                "label": label,
                "remote_host": remote_host,
                "remote_port": remote_port,
                "local_port": local_port,
            },
        )
    except CluError as exc:
        _fail(f"转发建立失败：{exc.message}")
        return
    console.print(
        f"[green]已建立[/green] 127.0.0.1:{row.get('local_port')}"
        f" -> {remote_host}:{remote_port}（{row.get('id')}）"
    )


@forward_app.command("list")
def forward_list() -> None:
    """列出当前转发。"""
    try:
        ensure_daemon()
        result = _do_request("forward.list", {})
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return
    raw = result.get("forwards")
    rows = [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if not rows:
        console.print("暂无转发")
        return
    table = Table(title="本地转发")
    for col in ("id", "connection", "local", "remote"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            str(row.get("id")),
            str(row.get("connection")),
            f"127.0.0.1:{row.get('local_port')}",
            f"{row.get('remote_host')}:{row.get('remote_port')}",
        )
    console.print(table)


@forward_app.command("remove")
def forward_remove(forward_id: str) -> None:
    """移除转发。"""
    try:
        ensure_daemon()
        _do_request("forward.remove", {"id": forward_id})
    except CluError as exc:
        _fail(f"移除失败：{exc.message}")
        return
    console.print(f"已移除 {forward_id}")


def cli_main() -> None:
    """console_scripts 入口。"""
    app()
