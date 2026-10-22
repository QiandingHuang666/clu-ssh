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


class FakeTmuxRunner:
    """有状态 fake：tmux 会话创建/存在/消亡 + salloc 日志授予。"""

    def __init__(self, alive: set[str] | None = None, granted: bool = True) -> None:
        self.sessions: set[str] = set(alive or set())
        self.granted = granted
        self.created: list[str] = []

    async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("scontrol"):
            return ExecResult(command, 0, NODES, "", 1)
        if command.startswith("tmux new-session"):
            self.created.append(command.split("-s ")[1].split(" ")[0].strip("'"))
            return ExecResult(command, 0, "", "", 1)
        if command.startswith("tmux has-session"):
            target = command.split("-t ")[1].strip()
            return ExecResult(command, 0 if target in self.sessions else 1, "", "", 1)
        if command.startswith("tmux kill-session"):
            self.sessions.discard(command.split("-t ")[1].strip())
            return ExecResult(command, 0, "", "", 1)
        if command.startswith("tmux list-sessions"):
            rows = "".join(f"{n}|1700000000|0\n" for n in self.sessions)
            return ExecResult(command, 0, rows, "", 1)
        if command.startswith("tmux pipe-pane"):
            self.sessions.update(self.created)  # 简化：pipe-pane 即视为会话就绪
            return ExecResult(command, 0, "", "", 1)
        if command.startswith("tail"):
            log = "salloc: Granted job allocation 7\n" if self.granted else ""
            return ExecResult(command, 0, log, "", 1)
        return ExecResult(command, 0, "", "", 1)


def make_runner() -> FakeTmuxRunner:
    return FakeTmuxRunner()


def test_build_salloc_gpus_and_cpus() -> None:
    cmd = alloc.build_salloc(purpose="train", partition="gpu", gpus=2, cpus=8, time_limit="4:00:00")
    assert "--gres=gpu:2" in cmd  # 内部方言转换
    assert "-c 8" in cmd
    assert cmd.endswith("srun --unbuffered --pty bash -l")


def test_describe_request_user_facing() -> None:
    assert "--gpus=2" in alloc.describe_request("gpu", 2, None, None)
    assert "-p x" in alloc.describe_request(None, 1, None, "x")
    assert "auto(gpu优先)" in alloc.describe_request(None, None, None, None)


async def test_rank_gpu_partitions_prefers_most_free() -> None:
    ranked, note, _hint = await alloc._rank_gpu_partitions(make_runner(), 1)  # type: ignore[arg-type]
    assert ranked[0] == "gcpu"  # ggpu 分区余 4 > gpu 分区余 2
    assert "gpu空" in note


async def test_rank_cpu_partitions_prefers_pure_cpu() -> None:
    ranked, _ = await alloc._rank_cpu_partitions(make_runner(), 4)  # type: ignore[arg-type]
    assert ranked[0] == "cpu"  # 纯 CPU 分区优先于带 GPU 的 gcpu
    assert "gcpu" in ranked  # 带回退候选


async def test_allocate_auto_prefers_gpu() -> None:
    runner = make_runner()
    info = await alloc.allocate(runner, "gzu", purpose="t", wait=1)
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


async def test_rank_gpu_partitions_respects_cpus() -> None:
    """GPU 够但 CPU 核数不足的节点必须排除。"""
    busy_cpu_nodes = NODES.replace("CPUAlloc=8", "CPUAlloc=63")  # g1 CPU 只剩 1 核

    # g1 被排除后只剩 gcpu（L20 余 4、CPU 空闲）
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("scontrol"):
            return ExecResult(command, 0, busy_cpu_nodes, "", 1)
        return ExecResult(command, 0, "", "", 1)

    ranked, _, _hint2 = await alloc._rank_gpu_partitions(run, 1, cpus=16)  # type: ignore[arg-type]
    assert ranked == ["gcpu"]


async def test_allocate_gpu_with_cpus() -> None:
    info = await alloc.allocate(make_runner(), "gzu", purpose="t9", gpus=2, cpus=16, wait=1)  # type: ignore[arg-type]
    assert "--cpus=16" in str(info["request"])
    assert info["cpus"] == 16


async def test_allocate_profile_default_cpus() -> None:
    """GPU 抢占未给 cpus 时应用 profile 默认核数。"""
    info = await alloc.allocate(make_runner(), "gzu", purpose="t10", wait=1, default_cpus=8)  # type: ignore[arg-type]
    assert "--cpus=8" in str(info["request"])
    assert info["cpus"] == 8


async def test_default_cpus_formula() -> None:
    """默认核数 = min(节点总核//总卡, 空闲核)：g1 64 核 8 卡 -> 8；gcpu 64 核 4 卡 -> 16。"""
    info = await alloc.allocate(make_runner(), "gzu", purpose="tf", wait=1)  # type: ignore[arg-type]
    assert info["cpus"] == 16  # 最优节点 gcpu：64//4=16（空闲 64 不设限）
    assert "--cpus=16" in str(info["request"])


async def test_default_cpus_capped_by_free() -> None:
    """空闲核不足时被空闲数封顶。"""

    class Capped(FakeTmuxRunner):
        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            if command.startswith("scontrol"):
                capped = NODES.replace(
                    "NodeName=gc1 CPUAlloc=0 CPUTot=64", "NodeName=gc1 CPUAlloc=60 CPUTot=64"
                )
                return ExecResult(command, 0, capped, "", 1)
            return await super().__call__(command, timeout=timeout)

    info = await alloc.allocate(Capped(), "gzu", purpose="tc", wait=1)
    assert info["cpus"] == 4  # min(64//4=16, 空闲 4) = 4


async def test_allocate_same_purpose_exists() -> None:
    """同名活会话：返回 exists 与指引，不创建新会话（用户裁决 B）。"""
    runner = FakeTmuxRunner(alive={"clu/gzu/train"})
    info = await alloc.allocate(runner, "gzu", purpose="train", partition="gpu", wait=1)
    assert info["state"] == "exists"
    assert info["job_id"] == "7"  # 探测到旧会话信息
    assert "attach" in str(info["error"]) and "session_release" in str(info["error"])
    assert runner.created == []  # 未创建新会话


async def test_allocate_same_purpose_dead_rebuilds() -> None:
    """同名死会话：正常重建。"""
    runner = FakeTmuxRunner(alive=set())
    info = await alloc.allocate(runner, "gzu", purpose="train", partition="gpu", wait=1)
    assert info["state"] == "granted"
    assert runner.created  # 创建了新会话


async def test_probe_last_granted_wins() -> None:
    """日志跨重建累积：必须取最后一次授予，否则重建后报旧 job_id（真机踩坑）。"""
    from clu.slurm.alloc import probe_session

    class AccumulatedLog(FakeTmuxRunner):
        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            if command.startswith("tail"):
                return ExecResult(
                    command,
                    0,
                    "salloc: Granted job allocation 111\nsalloc: Nodes old-1 are ready for job\n"
                    "salloc: Revoking...\nsalloc: Granted job allocation 222\n"
                    "salloc: Nodes new-9 are ready for job\n",
                    "",
                    1,
                )
            return await super().__call__(command, timeout=timeout)

    runner = AccumulatedLog(alive={"clu/gzu/train"})
    info = await probe_session(runner, "gzu", "train")
    assert info["job_id"] == "222"
    assert info["node"] == "new-9"


async def test_allocate_without_tmux() -> None:
    """远端没有 tmux：明确报错并建议 exec/submit。"""

    class NoTmux(FakeTmuxRunner):
        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            if command.startswith("command -v tmux"):
                return ExecResult(command, 1, "", "", 1)
            return await super().__call__(command, timeout=timeout)

    info = await alloc.allocate(NoTmux(), "box", purpose="t", partition="cpu", wait=1)
    assert info["state"] == "failed"
    assert "tmux" in str(info["error"]) and "exec" in str(info["error"])
