"""CLI 测试：入口、版本、命令组与本地命令（目录隔离）。"""

from pathlib import Path

from typer.testing import CliRunner

from clu import __version__
from clu.cli import app

runner = CliRunner()

SSH_CONFIG = """
Host t1
  HostName 127.0.0.1
  Port 2222
  User u
"""


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


def test_server_status_without_daemon_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(app, ["server", "status"])
    assert result.exit_code == 1
    assert "未运行" in result.output or "不可达" in result.output


def test_connection_import_then_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(tmp_path / "rt"))
    src = tmp_path / "ssh_config"
    src.write_text(SSH_CONFIG, encoding="utf-8")
    result = runner.invoke(app, ["connection", "import", "--config", str(src)])
    assert result.exit_code == 0
    assert "t1" in result.output
    result = runner.invoke(app, ["connection", "list"])
    assert result.exit_code == 0
    assert "t1" in result.output


def test_connection_list_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(tmp_path / "rt"))
    result = runner.invoke(app, ["connection", "list"])
    assert result.exit_code == 0
    assert "暂无连接" in result.output
