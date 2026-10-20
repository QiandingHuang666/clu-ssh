"""抢占接口测试：命令构造、分区自动选择（GPU 优先回退 CPU）、显式覆盖。"""

import pytest

import clu.slurm.alloc as alloc
from clu.ssh.connection import ExecResult

# 假 scontrol 数据：gpu 分区 8 卡已占 6（余 2）、gpu2 全占、cpu 分区空闲、gcpu 是带 GPU 的分区
NODES = (
    " NodeName=g1 CPUAlloc=8 CPUTot=64 State=MIXED AllocTRES=cpu=8,gres/gpu=6"
    " Gres=gpu:A100:8 Partitions=gpu \n"
    " NodeName=c1 CPUAlloc=0 CPUTot=48 State=IDLE Gres=(null) Partitions=cpu \n"
    " NodeName=gc1 CPUAlloc=0 CPUTot=64 State=IDLE Gres=gpu:L20:4 Partitions=gcpu \n"
)


def make_runner() -> object:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("scontrol"):
            return ExecResult(command, 0, NODES, "", 1)
        if command.startswith("tmux has-session"):
            return ExecResult(command, 0, "", "", 1)
        if command.startswith("tail"):
            return ExecResult(command, 0, "salloc: Granted job allocation 7\n", "", 1)
        return ExecResult(command, 0, "", "", 1)

    return run


def test_build_salloc_gpus_and_cpus() -> None:
    cmd = alloc.build_salloc(purpose="train", partition="gpu", gpus=2, cpus=8, time_limit="4:00:00")
    assert "--gres=gpu:2" in cmd  # 内部方言转换
    assert "-c 8" in cmd
    assert cmd.endswith("srun --unbuffered bash")


def test_describe_request_user_facing() -> None:
    assert "--gpus=2" in alloc.describe_request("gpu", 2, None, None)
    assert "-p x" in alloc.describe_request(None, 1, None, "x")
    assert "auto(gpu优先)" in alloc.describe_request(None, None, None, None)


async def test_rank_gpu_partitions_prefers_most_free() -> None:
    ranked, note = await alloc._rank_gpu_partitions(make_runner(), 1)  # type: ignore[arg-type]
    assert ranked[0] == "gcpu"  # ggpu 分区余 4 > gpu 分区余 2
    assert "gpu空" in note


async def test_rank_cpu_partitions_prefers_pure_cpu() -> None:
    ranked, _ = await alloc._rank_cpu_partitions(make_runner(), 4)  # type: ignore[arg-type]
    assert ranked[0] == "cpu"  # 纯 CPU 分区优先于带 GPU 的 gcpu
    assert "gcpu" in ranked  # 带回退候选


async def test_allocate_auto_prefers_gpu() -> None:
    info = await alloc.allocate(make_runner(), "gzu", purpose="t", wait=1)  # type: ignore[arg-type]
    assert info["state"] == "granted"
    assert info["chosen_by"] == "auto-gpu"
    assert info["partition"] == "gcpu"
    assert "--gpus=1" in str(info["request"])


async def test_allocate_explicit_cpu() -> None:
    info = await alloc.allocate(make_runner(), "gzu", purpose="t2", resource="cpu", cpus=8, wait=1)  # type: ignore[arg-type]
    assert info["state"] == "granted"
    assert info["partition"] == "cpu"
    assert "--cpus=8" in str(info["request"])


async def test_allocate_explicit_partition_skips_selection() -> None:
    info = await alloc.allocate(make_runner(), "gzu", purpose="t3", partition="cpu", wait=1)  # type: ignore[arg-type]
    assert info["chosen_by"] == "explicit"
    assert info["partition"] == "cpu"


async def test_allocate_gpu_unavailable_fails_with_availability() -> None:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("scontrol"):
            busy = NODES.replace("gres/gpu=6", "gres/gpu=8").replace(
                "Gres=gpu:L20:4", "Gres=(null)"
            )
            return ExecResult(command, 0, busy, "", 1)
        return ExecResult(command, 0, "", "", 1)

    info = await alloc.allocate(run, "gzu", purpose="t4", resource="gpu", wait=1)  # type: ignore[arg-type]
    assert info["state"] == "failed"
    assert "无可用 GPU" in str(info["error"])
    assert info.get("availability")


async def test_allocate_bad_resource_raises() -> None:
    with pytest.raises(ValueError, match="resource"):
        await alloc.allocate(make_runner(), "gzu", purpose="x", resource="tpu", wait=1)  # type: ignore[arg-type]


async def test_release() -> None:
    result = await alloc.release(make_runner(), "gzu", "train")  # type: ignore[arg-type]
    assert result["released"] is True
    assert result["session"] == "clu/gzu/train"
