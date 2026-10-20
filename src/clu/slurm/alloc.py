"""alloc = 抢占接口（DESIGN §6，语义按验收期用户裁决升级）：

- 申请必须直接执行（tmux 内 salloc），禁止只返回命令让用户手动跑
- 抢占选择在 daemon 内完成：默认（resource 缺省）GPU 优先、无空闲 GPU 则 CPU；
  显式 resource=gpu/cpu 只抢该类不回退；指定 partition 则直接用
- 分配后 `srun bash` 落计算节点交互 shell：attach 即入节点，exit 即释放
- 用户/agent 侧参数保持简洁（gpus=N / cpus=N）；集群方言（--gres vs --gpus）
  由 clu 内部转换，日志与留痕展示逻辑形式
"""

import asyncio
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import cast

import clu.slurm.commands as slurm
import clu.tmux as tmux
from clu.ssh.connection import ExecResult

Runner = Callable[..., Awaitable[ExecResult]]

GRANTED = re.compile(r"Granted job allocation (\d+)")
NODES = re.compile(r"Nodes (\S+) are ready for job")


def build_salloc(
    *,
    purpose: str,
    partition: str | None = None,
    gpus: int | None = None,
    cpus: int | None = None,
    time_limit: str | None = None,
) -> str:
    """构造 salloc 命令。

    --gres=gpu:N 为内部形式（贵大提交插件强制经典语法，且新旧 slurm 通吃）；
    对用户/agent 的展示形式是 gpus=N。
    """
    parts = ["salloc", "-J", f"clu-{purpose}"]
    if partition:
        parts += ["-p", partition]
    if gpus:
        parts += [f"--gres=gpu:{int(gpus)}"]
    if cpus:
        parts += ["-c", str(int(cpus))]
    if time_limit:
        parts += ["-t", time_limit]
    # srun bash：落到计算节点交互 shell（集群无 SallocDefaultCommand 时 salloc 默认
    # 本地执行，实测贵大如此）；attach 即入计算节点，exit 即释放资源
    parts += ["srun", "--unbuffered", "bash"]
    return " ".join(shlex.quote(p) for p in parts)


def describe_request(
    resource: str | None, gpus: int | None, cpus: int | None, partition: str | None
) -> str:
    """用户/agent 可读的简洁申请描述（--gpus 风格）。"""
    kind = resource or "auto(gpu优先)"
    parts = [f"resource={kind}"]
    if gpus:
        parts.append(f"--gpus={int(gpus)}")
    if cpus:
        parts.append(f"--cpus={int(cpus)}")
    if partition:
        parts.append(f"-p {partition}")
    return " ".join(parts)


def _rank_partitions(
    nodes: list[dict[str, object]], score: Callable[[dict[str, object]], int], want: int
) -> list[str]:
    """按单节点可满足度给分区排名（去重、最多 3 个候选）。"""
    ranked: list[tuple[int, str]] = []
    for n in nodes:
        node_score = score(n)
        if node_score < want:
            continue
        part_list = cast(list[str], n.get("partitions") or [])
        if not part_list:
            continue
        pname = part_list[0].rstrip("*")
        if all(pname != p for _, p in ranked):
            ranked.append((node_score, pname))
    ranked.sort(reverse=True)
    return [p for _, p in ranked[:3]]


def _default_cpus_formula(n: dict[str, object]) -> int:
    """GPU 节点默认核数（用户规定）：min(节点总核//节点总卡数, 节点空闲核)。"""
    cpu = cast(dict[str, int], n.get("cpu") or {})
    gpus_by_type = cast(dict[str, dict[str, int]], n.get("gpus") or {})
    total_gpus = sum(v["total"] for v in gpus_by_type.values())
    per_gpu = (int(cpu.get("total") or 0) // total_gpus) if total_gpus else 0
    return max(1, min(per_gpu, int(cpu.get("free") or 0)))


async def _formula_cpus_for_partition(runner: Runner, partition: str) -> int:
    """显式分区时按公式取该分区最优节点的默认核数。"""
    data = await slurm.resources(runner, gpu_only=True, flt="cpu_gpu_free", partition=partition)
    nodes = cast(list[dict[str, object]], data.get("nodes") or [])
    want = partition.rstrip("*")
    best, best_free = 0, 0
    for n in nodes:
        part_list = [p.rstrip("*") for p in cast(list[str], n.get("partitions") or [])]
        if want not in part_list:
            continue
        gpus_by_type = cast(dict[str, dict[str, int]], n.get("gpus") or {})
        free = sum(v["free"] for v in gpus_by_type.values())
        if free > best_free:
            best_free, best = free, _default_cpus_formula(n)
    return best


async def _rank_gpu_partitions(
    runner: Runner, gpus: int, cpus: int | None = None
) -> tuple[list[str], str, int]:
    """CPU 与 GPU 同时空闲、单节点 GPU 数与 CPU 核数都可满足的分区排名。

    返回 (候选分区, 余量摘要, 最优节点的默认核数建议)。
    """
    data = await slurm.resources(runner, gpu_only=True, flt="cpu_gpu_free")
    nodes = cast(list[dict[str, object]], data.get("nodes") or [])
    want_cpus = max(1, cpus or 1)

    def eligible(n: dict[str, object]) -> bool:
        cpu_free = int(cast(dict[str, int], n.get("cpu") or {}).get("free") or 0)
        return cpu_free >= want_cpus

    def score(n: dict[str, object]) -> int:
        gpus_by_type = cast(dict[str, dict[str, int]], n.get("gpus") or {})
        return sum(v["free"] for v in gpus_by_type.values())

    filtered = [n for n in nodes if eligible(n)]
    ranked = _rank_partitions(filtered, score, max(1, gpus))
    hint = 0
    if ranked:
        best_name = ranked[0]
        for n in filtered:
            part_list = cast(list[str], n.get("partitions") or [])
            if part_list and part_list[0].rstrip("*") == best_name and score(n) > 0:
                hint = _default_cpus_formula(n)
                break
    return ranked, _availability_note(data), hint


async def _rank_cpu_partitions(runner: Runner, cpus: int) -> tuple[list[str], str]:
    """单节点 CPU 余量可满足的分区排名；纯 CPU 分区优先。"""
    data = await slurm.resources(runner, flt="cpu_free")
    nodes = cast(list[dict[str, object]], data.get("nodes") or [])
    want = max(1, cpus or 1)

    def cpu_free_of(n: dict[str, object]) -> int:
        return int(cast(dict[str, int], n.get("cpu") or {}).get("free") or 0)

    pure = [n for n in nodes if not n.get("gpus")]
    mixed = [n for n in nodes if n.get("gpus")]
    ranked = _rank_partitions(pure, cpu_free_of, want)
    ranked += [p for p in _rank_partitions(mixed, cpu_free_of, want) if p not in ranked]
    return ranked[:3], _availability_note(data)


def _availability_note(data: dict[str, object]) -> str:
    partitions = cast(dict[str, dict[str, object]], data.get("partitions") or {})
    parts: list[str] = []
    for pname, p in partitions.items():
        gpu_types = cast(dict[str, dict[str, int]], p["gpus"])
        gpu_free = sum(v["free"] for v in gpu_types.values())
        cpu = cast(dict[str, int], p["cpu"])
        if gpu_free:
            parts.append(f"{pname}:gpu空{gpu_free}")
        if cpu["free"]:
            parts.append(f"{pname}:cpu空{cpu['free']}")
    return "; ".join(parts[:8]) or "无空闲资源"


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
    resource: str | None = None,
    gpus: int | None = None,
    cpus: int | None = None,
    partition: str | None = None,
    wait: float = 120.0,
    poll_interval: float = 2.0,
    time_limit: str | None = None,
    default_cpus: int | None = None,
) -> dict[str, object]:
    """抢占入口：直接执行 salloc 并轮询到授予/超时。

    - resource 缺省：GPU 优先，无空闲 GPU 分区则回退 CPU
    - resource=gpu / cpu：只抢该类，无则失败并附余量摘要（不回退）
    - partition 显式指定：直接使用，跳过自动选择
    """
    if resource not in (None, "gpu", "cpu"):
        raise ValueError(f"resource 必须是 gpu/cpu，收到：{resource}")
    # resource 缺省推断：只给 cpus 视为 CPU 意图，否则 GPU 优先（自动模式可回退）
    auto_mode = resource is None
    if auto_mode:
        resource = "cpu" if (not gpus and cpus) else "gpu"
    # CPU 抢占不带 GPU 请求（CLI 默认 gpus=1 不得渗入）
    gpus = (gpus or 1) if resource == "gpu" else None
    # GPU 抢占默认核数：显式 > profile 覆盖 > 节点公式 min(总核//总卡, 空闲核)
    # （公式值在分区选定后回填，见下方 rank 结果）
    shown_resource = "auto(gpu优先)" if auto_mode and resource == "gpu" else resource
    request_desc = describe_request(shown_resource, gpus, cpus, partition)
    chosen_by = "explicit" if partition is not None else ""
    note = ""

    candidates: list[str] = []
    note = ""
    if partition is not None:
        candidates = [partition]
    elif resource == "gpu":
        candidates, note, formula_cpus = await _rank_gpu_partitions(runner, int(gpus or 1), cpus)
        if cpus is None:
            cpus = int(default_cpus) if default_cpus else (formula_cpus or None)
        chosen_by = "auto-gpu"
        if not candidates:
            if not auto_mode:
                return {
                    "connection": label,
                    "purpose": purpose,
                    "state": "failed",
                    "error": "无可用 GPU（CPU 与 GPU 同时空闲的节点不足）",
                    "availability": note,
                    "request": request_desc,
                }
            resource = "cpu"  # 自动模式回退
    if partition is not None and resource == "gpu" and cpus is None:
        cpus = await _formula_cpus_for_partition(runner, partition) or None
    if partition is None and resource == "cpu":
        cpu_candidates, cpu_note = await _rank_cpu_partitions(runner, cpus or 4)
        candidates = candidates or cpu_candidates
        note = note or cpu_note
        chosen_by = chosen_by if chosen_by == "explicit" else "auto-cpu"
        if not candidates:
            return {
                "connection": label,
                "purpose": purpose,
                "state": "failed",
                "error": "无可用 CPU 资源（GPU 亦无空闲）" if auto_mode else "无可用 CPU 资源",
                "availability": note,
                "request": request_desc,
            }

    request_desc = describe_request(shown_resource, gpus, cpus, partition)
    name = tmux.session_name(label, purpose)
    await runner("mkdir -p $HOME/.clu/alloc/" + shlex.quote(label))
    # 瞬死故障转移：提交被弹回/秒级收回（实测贵大忙分区竞态）时
    # 自动重试并轮换候选分区，单分区最多 2 次
    deadline = time.monotonic() + wait
    info: dict[str, object] = {}
    partition = candidates[0] if candidates else partition
    queue: list[str] = list(candidates)
    for _ in range(2):
        queue.extend(candidates)
    while queue:
        partition = queue.pop(0)
        salloc_cmd = build_salloc(
            purpose=purpose, partition=partition, gpus=gpus, cpus=cpus, time_limit=time_limit
        )
        created_at = time.monotonic()
        await tmux.create_session(runner, name, salloc_cmd, log_file=tmux.log_path(label, purpose))
        while True:
            info = await _probe(runner, label, purpose)
            if info["state"] != "pending":
                break
            if time.monotonic() >= deadline:
                return dict(
                    info,
                    request=request_desc,
                    partition=partition,
                    gpus=gpus,
                    cpus=cpus,
                    chosen_by=chosen_by,
                    note=f"等待 {wait:.0f}s 未授予，会话保留在 tmux 中，稍后用 session_info 查看",
                )
            await asyncio.sleep(poll_interval)
        instant_death = info["state"] == "failed" and (time.monotonic() - created_at) < 5.0
        if not instant_death:
            break
        await asyncio.sleep(1.0)

    chosen_by = chosen_by or ("auto-cpu" if resource == "cpu" else "auto-gpu")
    if info["state"] == "granted" and not info.get("node"):
        await asyncio.sleep(1.0)  # Nodes ready 行略晚于 Granted，补读一次
        info = await _probe(runner, label, purpose)
    return dict(
        info,
        request=request_desc,
        partition=partition,
        gpus=gpus,
        cpus=cpus,
        chosen_by=chosen_by,
    )


async def session_info(runner: Runner, label: str, purpose: str) -> dict[str, object]:
    return await _probe(runner, label, purpose, log_tail=100)


async def release(runner: Runner, label: str, purpose: str) -> dict[str, object]:
    """释放：杀 tmux 会话 → salloc 退出 → 分配归还。"""
    name = tmux.session_name(label, purpose)
    killed = await tmux.kill_session(runner, name)
    return {"connection": label, "purpose": purpose, "session": name, "released": killed}
