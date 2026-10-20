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
app.add_typer(connection_app, name="connection")
app.add_typer(server_app, name="server")
app.add_typer(session_app, name="session")

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


def cli_main() -> None:
    """console_scripts 入口。"""
    app()
