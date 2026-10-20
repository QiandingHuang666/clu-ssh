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


def test_resources_cmd_renders(monkeypatch) -> None:
    """resources CLI：mock RPC 返回，验证表格渲染路径。"""
    import clu.cli as cli_mod

    def fake_do_request(method: str, params: dict) -> dict:
        assert method == "slurm.resources"
        return {
            "partitions": {
                "gpu": {
                    "nodes": 2,
                    "idle_nodes": 1,
                    "cpu": {"total": 128, "allocated": 8, "free": 120},
                    "gpus": {"a100": {"total": 16, "allocated": 2, "free": 14}},
                },
            },
            "nodes": [
                {
                    "node": "gpu01",
                    "state": "MIXED",
                    "usable": True,
                    "fully_idle": False,
                    "cpu": {"total": 64, "allocated": 8, "free": 56},
                    "gpus": {"a100": {"total": 8, "allocated": 2, "free": 6}},
                    "partitions": ["gpu"],
                    "mem_mb": {"total": 1, "allocated": 0},
                },
            ],
            "note": "测试",
        }

    monkeypatch.setattr(cli_mod, "_do_request", fake_do_request)
    monkeypatch.setattr(cli_mod, "ensure_daemon", lambda: None)
    result = runner.invoke(app, ["resources", "cl", "--nodes"])
    assert result.exit_code == 0
    assert "gpu" in result.output and "14/16" in result.output and "gpu01" in result.output
