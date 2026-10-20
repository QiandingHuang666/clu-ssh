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
app.add_typer(connection_app, name="connection")
app.add_typer(server_app, name="server")

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


def cli_main() -> None:
    """console_scripts 入口。"""
    app()
