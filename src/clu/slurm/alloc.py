"""alloc 语义（DESIGN §6，继承旧 skill，不可削弱）：

- 申请资源必须直接执行（tmux 内 salloc），禁止只返回命令让用户手动跑
- 获得分配后 `sleep infinity` 占住资源，断线不亡
- "抢占 A100"等表述 → preempt=True：分区默认取 profile.preempt_partition
"""

import asyncio
import re
import shlex
import time
from collections.abc import Awaitable, Callable

import clu.tmux as tmux
from clu.profiles import Profile
from clu.ssh.connection import ExecResult

Runner = Callable[..., Awaitable[ExecResult]]

GRANTED = re.compile(r"Granted job allocation (\d+)")
NODES = re.compile(r"Nodes (\S+) are ready for job")


def build_salloc(
    *,
    purpose: str,
    partition: str | None = None,
    gpus: int | None = None,
    time_limit: str | None = None,
) -> str:
    """构造 salloc 命令：分配后跑 sleep infinity 保活。"""
    parts = ["salloc", "-J", f"clu-{purpose}"]
    if partition:
        parts += ["-p", partition]
    if gpus:
        parts += [f"--gpus={int(gpus)}"]
    if time_limit:
        parts += ["-t", time_limit]
    parts += ["sh", "-c", "sleep infinity"]  # sh 而非 bash：兼容 busybox 最小镜像
    return " ".join(shlex.quote(p) for p in parts)


async def _probe(
    runner: Runner, label: str, purpose: str, *, log_tail: int = 50
) -> dict[str, object]:
    """读取会话现状：tmux 存在性 + 日志中的分配信息。"""
    name = tmux.session_name(label, purpose)
    if not await tmux.has_session(runner, name):
        log = await tmux.read_log(runner, label, purpose)
        error = "tmux 会话已退出（分配失败或被杀）"
        if log:
            error += f"；日志尾部：{log.strip().splitlines()[-1][:200]}"
        return {
            "connection": label,
            "purpose": purpose,
            "session": name,
            "state": "failed",
            "error": error,
        }
    log = await tmux.read_log(runner, label, purpose, tail=log_tail)
    granted = GRANTED.search(log)
    node = NODES.search(log)
    if granted is None:
        return {
            "connection": label,
            "purpose": purpose,
            "session": name,
            "state": "pending",
            "log_tail": log.strip()[-500:],
        }
    return {
        "connection": label,
        "purpose": purpose,
        "session": name,
        "state": "granted",
        "job_id": granted.group(1),
        "node": node.group(1) if node else None,
    }


async def allocate(
    runner: Runner,
    label: str,
    *,
    purpose: str,
    partition: str | None = None,
    gpus: int | None = None,
    preempt: bool = False,
    profile: Profile | None = None,
    wait: float = 120.0,
    poll_interval: float = 2.0,
) -> dict[str, object]:
    """直接执行 alloc：建 tmux 会话跑 salloc，轮询日志直到授予/超时。"""
    if preempt and not partition and profile is not None and profile.preempt_partition:
        partition = profile.preempt_partition
    name = tmux.session_name(label, purpose)
    salloc = build_salloc(purpose=purpose, partition=partition, gpus=gpus)
    await runner("mkdir -p $HOME/.clu/alloc/" + shlex.quote(label))
    await tmux.create_session(runner, name, salloc, log_file=tmux.log_path(label, purpose))
    deadline = time.monotonic() + wait
    while True:
        info = await _probe(runner, label, purpose)
        if info["state"] != "pending":
            return dict(info, partition=partition, gpus=gpus, preempt=preempt)
        if time.monotonic() >= deadline:
            return dict(
                info,
                partition=partition,
                gpus=gpus,
                preempt=preempt,
                note=f"等待 {wait:.0f}s 未授予，会话保留在 tmux 中，稍后用 session_info 查看",
            )
        await asyncio.sleep(poll_interval)


async def session_info(runner: Runner, label: str, purpose: str) -> dict[str, object]:
    return await _probe(runner, label, purpose, log_tail=100)


async def release(runner: Runner, label: str, purpose: str) -> dict[str, object]:
    """释放：杀 tmux 会话 → salloc 退出 → 分配归还。"""
    name = tmux.session_name(label, purpose)
    killed = await tmux.kill_session(runner, name)
    return {"connection": label, "purpose": purpose, "session": name, "released": killed}
