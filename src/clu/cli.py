"""clu 命令行入口：daemon 的薄客户端；连接配置管理为本地文件操作。"""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from clu import __version__
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
        typer.echo(__version__)
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


@session_app.command("list")
def session_list(label: str = typer.Argument(None, help="连接 label；缺省列出全部。")) -> None:
    """列出 tmux 托管会话（alloc 资源会话）。"""
    try:
        ensure_daemon()
        result = _do_request("session.list", {"label": label})
    except CluError as exc:
        _fail(f"查询失败：{exc.message}")
        return
    raw_sessions = result.get("sessions")
    sessions = (
        [dict(r) for r in raw_sessions if isinstance(r, dict)]
        if isinstance(raw_sessions, list)
        else []
    )
    if not sessions:
        console.print("暂无托管会话")
        return
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
