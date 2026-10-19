"""CLI 骨架测试：入口、版本、命令组注册。"""

from typer.testing import CliRunner

from clu import __version__
from clu.cli import app

runner = CliRunner()


def test_help_lists_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for word in ("connection", "server"):
        assert word in result.output


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_server_subcommands_registered() -> None:
    result = runner.invoke(app, ["server", "--help"])
    assert result.exit_code == 0
    for word in ("ensure", "status", "stop"):
        assert word in result.output


def test_unimplemented_commands_exit_nonzero() -> None:
    result = runner.invoke(app, ["server", "status"])
    assert result.exit_code == 2
    assert "M1" in result.output
