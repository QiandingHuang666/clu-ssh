"""clu 命令行入口：所有命令都是 daemon 的薄客户端（M0 仅骨架）。"""

import typer

from clu import __version__

# invoke_without_command：允许 clu --version 这类无子命令调用先走 callback
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


def _planned(milestone: str) -> None:
    typer.secho(f"尚未实现：计划于 {milestone}（见 DESIGN.md）。", fg="yellow")
    raise typer.Exit(code=2)


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="显示版本并退出。"),
) -> None:
    """clu：连接池与集群操作的统一入口。"""
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@connection_app.command("import")
def connection_import() -> None:
    """从 ~/.ssh/config 一次性导入公钥连接（副本，解析 ProxyJump）。"""
    _planned("M1")


@connection_app.command("list")
def connection_list() -> None:
    """列出已管理的连接及其池状态。"""
    _planned("M1")


@server_app.command("ensure")
def server_ensure() -> None:
    """确保 daemon 运行中；不在则自动拉起。"""
    _planned("M1")


@server_app.command("status")
def server_status() -> None:
    """查看 daemon 与连接池状态。"""
    _planned("M1")


@server_app.command("stop")
def server_stop() -> None:
    """停止 daemon。"""
    _planned("M1")


def cli_main() -> None:
    """console_scripts 入口。"""
    app()
