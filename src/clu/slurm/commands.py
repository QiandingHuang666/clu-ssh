"""Slurm 命令层：squeue/sinfo/scontrol/sbatch 封装为结构化输出（DESIGN §6）。

设计原则：一次用户问题 = 一次远端往返。分区表/队列计数/资源余量
都用合并命令（分隔符分段）取回，daemon 侧解析聚合。
"""

import base64
import re
import shlex
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from clu.ssh.connection import CluConnectionError, ExecResult

Runner = Callable[..., Awaitable[ExecResult]]

JOBS_FIELDS = [
    "job_id",
    "state",
    "name",
    "partition",
    "elapsed",
    "time_limit",
    "nodes",
    "cpus",
    "mem",
    "gres",
    "reason",
]
_JOBS_FORMAT = "%i|%T|%j|%P|%M|%L|%D|%C|%m|%b|%R"

PARTITION_FIELDS = ["partition", "availability", "nodes", "state", "cpus", "mem", "gres"]
_PARTITION_FORMAT = "%P|%a|%D|%T|%C|%m|%G"

_SEPARATOR = "---CLUSEP---"
# 两段：sinfo 分区表 | 全队列状态
_STATUS_CMD = f"sinfo -h -o '{_PARTITION_FORMAT}'; echo {_SEPARATOR}; squeue -h -o '%T'"
# 节点明细单命令；AllocTRES 为权威已占数据源（squeue %b 在部分集群恒为 N/A）
_RESOURCES_CMD = "scontrol show nodes -o"

_SBATCH_JOB_ID = re.compile(r"Submitted batch job (\d+)")


async def _run(runner: Runner, command: str, *, timeout: float = 30.0) -> ExecResult:
    return await runner(command, timeout=timeout)


def _sections(stdout: str) -> list[list[str]]:
    return [part.splitlines() for part in stdout.split(_SEPARATOR)]


# ---- 作业 ----


async def list_jobs(
    runner: Runner, user: str, states: list[str] | None = None
) -> list[dict[str, str]]:
    """当前用户的作业列表（squeue，显式格式）。"""
    result = await _run(runner, f"squeue -u {shlex.quote(user)} -h -o '{_JOBS_FORMAT}'")
    jobs: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != len(JOBS_FIELDS):
            continue
        job = dict(zip(JOBS_FIELDS, parts, strict=True))
        if states and job["state"] not in states:
            continue
        jobs.append(job)
    return jobs


def _parse_partitions(lines: list[str]) -> list[dict[str, str]]:
    partitions: list[dict[str, str]] = []
    for line in lines:
        parts = line.strip().split("|")
        if len(parts) != len(PARTITION_FIELDS):
            continue
        partitions.append(dict(zip(PARTITION_FIELDS, parts, strict=True)))
    return partitions


async def cluster_status(runner: Runner) -> dict[str, object]:
    """分区/节点/GPU 概览（sinfo）+ 队列状态计数（squeue），单次往返。"""
    parts = _sections((await _run(runner, _STATUS_CMD)).stdout)
    partitions = _parse_partitions(parts[0] if parts else [])
    counts = Counter(line.strip() for line in (parts[1] if len(parts) > 1 else []) if line.strip())
    return {"partitions": partitions, "queue": dict(counts)}


# ---- 资源余量（CPU/GPU，节点级 + 分区级） ----

_HOSTLIST_TOKEN = re.compile(r"^(.*?)\[([0-9,\-]+)\](.*)$")
_GRES_CLEAN = re.compile(r"\(S:[^)]*\)")
_UNUSABLE = {"DOWN", "DRAIN", "DRAINING", "MAINT", "RESERVED", "FUTURE"}


@dataclass
class _Triple:
    total: int = 0
    allocated: int = 0
    free: int = 0

    def add(self, other: "_Triple") -> None:
        self.total += other.total
        self.allocated += other.allocated
        self.free += other.free

    def as_dict(self) -> dict[str, int]:
        return {"total": self.total, "allocated": self.allocated, "free": self.free}


@dataclass
class _PartAgg:
    nodes: int = 0
    idle_nodes: int = 0
    cpu: _Triple = field(default_factory=_Triple)
    gpus: dict[str, _Triple] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "nodes": self.nodes,
            "idle_nodes": self.idle_nodes,
            "cpu": self.cpu.as_dict(),
            "gpus": {k: v.as_dict() for k, v in sorted(self.gpus.items())},
        }


@dataclass
class _NodeRow:
    node: str
    partitions: list[str]
    state: str
    usable: bool
    fully_idle: bool
    cpu: _Triple
    mem_mb: dict[str, int]
    gpus: dict[str, _Triple]

    def as_dict(self) -> dict[str, object]:
        return {
            "node": self.node,
            "partitions": self.partitions,
            "state": self.state,
            "usable": self.usable,
            "fully_idle": self.fully_idle,
            "cpu": self.cpu.as_dict(),
            "mem_mb": self.mem_mb,
            "gpus": {k: v.as_dict() for k, v in sorted(self.gpus.items())},
        }


def _split_top_level(spec: str) -> list[str]:
    """顶层逗号分割（跳过方括号内的逗号）。"""
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in spec:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            tokens.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def expand_hostlist(spec: str) -> list[str]:
    """展开 Slurm hostlist：node[1-3,5] -> [node1, node2, node3, node5]。"""
    nodes: list[str] = []
    for token in _split_top_level(spec):
        token = token.strip()
        if not token:
            continue
        matched = _HOSTLIST_TOKEN.match(token)
        if matched is None:
            nodes.append(token)
            continue
        prefix, ranges, suffix = matched.groups()
        for piece in ranges.split(","):
            if "-" in piece:
                lo, hi = piece.split("-", 1)
                width = len(lo)
                for i in range(int(lo), int(hi) + 1):
                    nodes.append(f"{prefix}{i:0{width}d}{suffix}")
            else:
                nodes.append(f"{prefix}{piece}{suffix}")
    return nodes


def parse_gres(gres: str) -> dict[str, int]:
    """解析 GRES 串：'gpu:A100:8(S:0-1),gpu:4090:2' -> {'A100': 8, '4090': 2}。

    非 gpu 资源忽略；无型号记为 '_'。
    """
    result: dict[str, int] = {}
    for item in gres.split(","):
        item = _GRES_CLEAN.sub("", item).strip()
        if not item or item == "(null)":
            continue
        fields = item.split(":")
        if fields[0] != "gpu":
            continue
        if len(fields) >= 3 and fields[2].isdigit():
            key = fields[1] or "_"
            result[key] = result.get(key, 0) + int(fields[2])
        elif len(fields) == 2 and fields[1].isdigit():
            result["_"] = result.get("_", 0) + int(fields[1])
    return result


def _parse_kv(text: str) -> dict[str, str]:
    """scontrol -o 的 key=value 空格分隔输出。"""
    fields: dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if sep and key and key not in fields:
            fields[key] = value
    return fields


def _distribute(count: int, nodes: list[str]) -> dict[str, int]:
    """把作业级资源量摊到其节点上（--gpus=N 跨节点均分的估算）。"""
    if not nodes:
        return {}
    base, rem = divmod(count, len(nodes))
    per = dict.fromkeys(nodes, base)
    for n in nodes[:rem]:
        per[n] += 1
    return per


def _gpu_allocated_by_node(job_lines: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for line in job_lines:
        fields = line.strip().split("|")
        if len(fields) != 3:
            continue
        nodes = expand_hostlist(fields[0])
        for gpu_type, count in parse_gres(fields[1]).items():
            for node, part in _distribute(count, nodes).items():
                bucket = out.setdefault(node, {})
                bucket[gpu_type] = bucket.get(gpu_type, 0) + part
    return out


def _alloc_tres_gpus(alloc_tres: str, gres_total: dict[str, int]) -> dict[str, int]:
    """解析 AllocTRES 的 GPU 已占：优先带型号（gres/gpu:a10=1），
    无型号（gres/gpu=2）按节点型号摊（单型号精确，多型号均摊）。"""
    typed: dict[str, int] = {}
    typeless = 0
    for item in alloc_tres.split(","):
        item = item.strip()
        if item.startswith("gres/gpu:"):
            key, _, value = item.partition("=")
            if value.isdigit():
                gpu_type = key.partition(":")[2] or "_"
                typed[gpu_type] = typed.get(gpu_type, 0) + int(value)
        elif item.startswith("gres/gpu="):
            value = item.partition("=")[2]
            if value.isdigit():
                typeless += int(value)
    if typed:
        return typed
    if typeless and gres_total:
        types = list(gres_total)
        base, rem = divmod(typeless, len(types))
        out = dict.fromkeys(types, base)
        for t in types[:rem]:
            out[t] += 1
        return out
    return {}


async def resources(
    runner: Runner,
    *,
    partition: str | None = None,
    only_free: bool = False,
    only_idle: bool = False,
    include_nodes: bool = False,
) -> dict[str, object]:
    """节点级 + 分区级的 CPU/GPU 余量，单次往返。

    - CPU 已占取 scontrol CPUAlloc（权威值）；GPU 已占按运行中作业 gres 摊到节点（估算）
    - only_free：只看有空闲 CPU 或 GPU 的可用节点；only_idle：只看完全空闲（IDLE）节点
    - partition：按分区过滤（节点属多分区时命中即算）
    """
    node_lines = (await _run(runner, _RESOURCES_CMD, timeout=60.0)).stdout.splitlines()
    # AllocTRES（slurm 19.05+）为权威已占来源；缺失时退回运行作业估算
    has_alloc_tres = any("AllocTRES=" in line for line in node_lines)
    gpu_alloc: dict[str, dict[str, int]] = {}
    if not has_alloc_tres:
        legacy = await _run(runner, "squeue -h -t R -o '%N|%b'")
        gpu_alloc = _gpu_allocated_by_node(legacy.stdout.splitlines())

    rows: list[_NodeRow] = []
    for line in node_lines:
        f = _parse_kv(line)
        name = f.get("NodeName")
        if not name:
            continue
        state = f.get("State", "UNKNOWN")
        base_state = state.split("+", 1)[0].rstrip("*~#%")
        usable = base_state not in _UNUSABLE
        cpu = _Triple(
            total=int(f.get("CPUTot", "0") or 0),
            allocated=int(f.get("CPUAlloc", "0") or 0),
        )
        cpu.free = max(0, cpu.total - cpu.allocated)
        gres_total = parse_gres(f.get("Gres", ""))
        if has_alloc_tres:
            allocated = _alloc_tres_gpus(f.get("AllocTRES", ""), gres_total)
        else:
            allocated = gpu_alloc.get(name, {})
        gpus: dict[str, _Triple] = {}
        for gpu_type, total in gres_total.items():
            used = allocated.get(gpu_type, 0)
            gpus[gpu_type] = _Triple(total=total, allocated=used, free=max(0, total - used))
        rows.append(
            _NodeRow(
                node=name,
                partitions=[p for p in f.get("Partitions", "").split(",") if p],
                state=state,
                usable=usable,
                fully_idle=usable and base_state == "IDLE",
                cpu=cpu,
                mem_mb={
                    "total": int(f.get("RealMemory", "0") or 0),
                    "allocated": int(f.get("AllocMem", "0") or 0),
                },
                gpus=gpus,
            )
        )

    # 过滤
    if partition:
        want = partition.rstrip("*")
        rows = [r for r in rows if want in [p.rstrip("*") for p in r.partitions]]
    if only_idle:
        rows = [r for r in rows if r.fully_idle]
    elif only_free:
        rows = [
            r
            for r in rows
            if r.usable and (r.cpu.free > 0 or any(g.free > 0 for g in r.gpus.values()))
        ]

    # 分区聚合（基于过滤后集合，与返回的节点列表口径一致）
    agg: dict[str, _PartAgg] = {}
    for r in rows:
        for raw in r.partitions:
            pname = raw.rstrip("*")
            bucket = agg.setdefault(pname, _PartAgg())
            bucket.nodes += 1
            if r.fully_idle:
                bucket.idle_nodes += 1
            bucket.cpu.add(r.cpu)
            for gpu_type, g in r.gpus.items():
                bucket.gpus.setdefault(gpu_type, _Triple()).add(g)

    node_payload: list[dict[str, object]] = []
    if include_nodes or partition or only_free or only_idle:
        node_payload = [r.as_dict() for r in rows]
    return {
        "nodes": node_payload,
        "node_count": len(rows),
        "partitions": {k: v.as_dict() for k, v in sorted(agg.items())},
        "filters": {"partition": partition, "only_free": only_free, "only_idle": only_idle},
        "note": (
            "GPU/CPU 已占取节点 AllocTRES（权威值）"
            if has_alloc_tres
            else "本集群无 AllocTRES：GPU 已占按运行作业估算"
        ),
    }


# ---- 作业日志 / 提交 / 取消 ----


async def job_log(runner: Runner, job_id: str, *, tail: int = 100) -> dict[str, object]:
    """读作业 stdout：scontrol 找到 StdOut 路径后 tail。"""
    info = await _run(runner, f"scontrol show job {shlex.quote(job_id)}")
    fields = _parse_kv(info.stdout)
    path = fields.get("StdOut", "")
    result: dict[str, object] = {
        "job_id": job_id,
        "state": fields.get("JobState", "UNKNOWN"),
        "stdout_path": path,
        "text": "",
        "exists": False,
    }
    if not path or path == "(null)":
        result["text"] = f"作业 {job_id} 尚无输出文件（state={result['state']}）"
        return result
    cat = await _run(runner, f"tail -n {int(tail)} {shlex.quote(path)} 2>/dev/null; echo CLUEOF$?")
    lines = cat.stdout.rsplit("CLUEOF", 1)
    result["text"] = lines[0]
    result["exists"] = lines[1].strip() == "0" if len(lines) == 2 else False
    return result


async def submit(runner: Runner, script: str) -> dict[str, object]:
    """上传脚本（base64 防注入）并 sbatch，返回 job_id。"""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    marker = uuid.uuid4().hex[:12]
    remote_dir = "$HOME/.clu/jobs"
    command = (
        f"mkdir -p {remote_dir} && echo {encoded} | base64 -d > {remote_dir}/job-{marker}.sh"
        f" && sbatch {remote_dir}/job-{marker}.sh"
    )
    result = await _run(runner, command)
    matched = _SBATCH_JOB_ID.search(result.stdout)
    if result.exit_code != 0 or matched is None:
        raise CluConnectionError(
            f"sbatch 失败（exit={result.exit_code}）：{result.stderr.strip()[:500]}"
        )
    return {"job_id": matched.group(1), "script_path": f"{remote_dir}/job-{marker}.sh"}


async def cancel(runner: Runner, job_id: str) -> dict[str, object]:
    result = await _run(runner, f"scancel {shlex.quote(job_id)}")
    return {"job_id": job_id, "exit_code": result.exit_code, "stderr": result.stderr.strip()}
