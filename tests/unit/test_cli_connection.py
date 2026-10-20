"""connection add/remove CLI 测试（目录隔离）。"""

from pathlib import Path

from typer.testing import CliRunner

from clu.cli import app

runner = CliRunner()


def test_add_then_list_then_remove(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(tmp_path / "rt"))
    result = runner.invoke(
        app,
        [
            "connection",
            "add",
            "gzu",
            "--host",
            "hpc.example.edu.cn",
            "--user",
            "you",
            "--port",
            "22",
            "--kind",
            "cluster",
        ],
    )
    assert result.exit_code == 0
    assert "已添加" in result.output

    result = runner.invoke(app, ["connection", "list"])
    assert "gzu" in result.output

    result = runner.invoke(app, ["connection", "remove", "gzu"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["connection", "list"])
    assert "暂无连接" in result.output


def test_add_rejects_bad_kind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLU_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLU_RUNTIME_DIR", str(tmp_path / "rt"))
    result = runner.invoke(
        app, ["connection", "add", "x", "--host", "h", "--user", "u", "--kind", "wrong"]
    )
    assert result.exit_code == 1
