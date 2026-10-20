"""tmux 编排与 alloc 语义测试（fake runner）。"""

import pytest

import clu.slurm.alloc as alloc
import clu.tmux as tmux
from clu.profiles import Profile
from clu.ssh.connection import ExecResult


def make_runner(script: dict[str, str] | object = None, default: str = "") -> object:
    """命令前缀匹配返回脚本输出。"""

    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if isinstance(script, dict):
            for prefix, out in script.items():
                if command.startswith(prefix):
                    return ExecResult(command, 0, out, "", 1)
        return ExecResult(command, 0, default, "", 1)

    return run


def test_session_name_roundtrip() -> None:
    name = tmux.session_name("gzu", "train-a100")
    assert name == "clu/gzu/train-a100"
    assert tmux.parse_session_name(name) == ("gzu", "train-a100")
    assert tmux.parse_session_name("other/session") is None


def test_session_name_rejects_bad_parts() -> None:
    with pytest.raises(ValueError, match="非法"):
        tmux.session_name("a/b", "x")
    with pytest.raises(ValueError, match="非法"):
        tmux.session_name("a", "x:y")


def test_build_salloc() -> None:
    cmd = alloc.build_salloc(purpose="train", partition="gpu", gpus=2, time_limit="4:00:00")
    assert cmd.startswith("salloc -J clu-train -p gpu --gpus=2 -t 4:00:00")
    assert cmd.endswith("sh -c 'sleep infinity'")
    assert "sleep infinity" in cmd


async def test_probe_granted() -> None:
    runner = make_runner(
        {
            "tmux has-session": "",
            "tail": "salloc: Granted job allocation 42\nsalloc: Nodes n1 are ready for job",
        }
    )
    info = await alloc._probe(runner, "gzu", "train")  # type: ignore[arg-type]
    assert info["state"] == "granted"
    assert info["job_id"] == "42"
    assert info["node"] == "n1"


async def test_probe_failed_when_session_gone() -> None:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("tmux has-session"):
            return ExecResult(command, 1, "", "", 1)
        return ExecResult(command, 0, "", "", 1)

    info = await alloc._probe(run, "gzu", "train")
    assert info["state"] == "failed"


async def test_allocate_granted_and_preempt_partition() -> None:
    """端到端 fake：建会话 -> 探测到授予；preempt 无显式分区时取 profile。"""
    runner = make_runner(
        {
            "tmux has-session": "",
            "tail": "salloc: Granted job allocation 7",
            "mkdir": "",
            "tmux new-session": "",
        }
    )
    profile = Profile(label="gzu", preempt_partition="preempt-gpu")
    info = await alloc.allocate(
        runner,  # type: ignore[arg-type]
        "gzu",
        purpose="train",
        preempt=True,
        profile=profile,
        wait=5,
    )
    assert info["state"] == "granted"
    assert info["job_id"] == "7"
    assert info["partition"] == "preempt-gpu"
    assert info["preempt"] is True


async def test_allocate_pending_times_out_with_note() -> None:
    runner = make_runner(
        {"tmux has-session": "", "tail": "salloc: Pending...", "mkdir": "", "tmux new-session": ""}
    )
    info = await alloc.allocate(
        runner,  # type: ignore[arg-type]
        "gzu",
        purpose="slow",
        wait=0.1,
        poll_interval=0.05,
    )
    assert info["state"] == "pending"
    assert "session_info" in str(info.get("note"))


async def test_release() -> None:
    runner = make_runner({"tmux kill-session": ""})
    result = await alloc.release(runner, "gzu", "train")  # type: ignore[arg-type]
    assert result["released"] is True
    assert result["session"] == "clu/gzu/train"
