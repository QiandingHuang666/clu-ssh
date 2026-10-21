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


def test_session_release_cli(monkeypatch) -> None:
    import clu.cli as cli_mod

    calls = []

    def fake_do_request(method: str, params: dict) -> dict:
        calls.append((method, params))
        if method == "session.release":
            assert params["purpose"] in ("train", "eval")
            return {"released": True}
        return {"sessions": [{"purpose": "train"}, {"purpose": "eval"}]}

    monkeypatch.setattr(cli_mod, "_do_request", fake_do_request)
    monkeypatch.setattr(cli_mod, "ensure_daemon", lambda: None)

    result = runner.invoke(app, ["session", "release", "cl", "train"])
    assert result.exit_code == 0
    assert "已释放" in result.output

    result = runner.invoke(app, ["session", "release", "cl", "--all"])
    assert result.exit_code == 0
    assert calls.count(("session.release", {"label": "cl", "purpose": "eval"})) == 1

    result = runner.invoke(app, ["session", "release", "cl"])
    assert result.exit_code == 1  # 缺 PURPOSE 且无 --all


def test_jobs_cmd_renders(monkeypatch) -> None:
    import clu.cli as cli_mod

    def fake_do_request(method: str, params: dict) -> dict:
        assert method == "slurm.jobs"
        assert params["states"] == ["RUNNING"]
        return {
            "jobs": [
                {
                    "job_id": "12345",
                    "state": "RUNNING",
                    "name": "train",
                    "partition": "gpu",
                    "elapsed": "01:00",
                    "time_limit": "4:00:00",
                    "nodes": "1",
                    "cpus": "8",
                    "gres": "gpu:A100:1",
                    "reason": "None",
                }
            ]
        }

    monkeypatch.setattr(cli_mod, "_do_request", fake_do_request)
    monkeypatch.setattr(cli_mod, "ensure_daemon", lambda: None)
    result = runner.invoke(app, ["jobs", "cl", "--states", "running"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "12345" in result.output and "RUNNING" in result.output
