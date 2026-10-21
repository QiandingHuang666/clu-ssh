"""Slurm 命令层：squeue/sinfo/scontrol/sbatch 封装为结构化输出（DESIGN §6）。

设计原则：一次用户问题 = 一次远端往返。分区表/队列计数/资源余量
都用合并命令（分隔符分段）取回，daemon 侧解析聚合。
"""

import base64
import re
import shlex
import time
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
_SBATCH_LINE = re.compile(r"^\s*#SBATCH\s+", re.IGNORECASE)

TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)


def normalize_state(raw: str) -> str:
    """squeue/sacct 状态归一：去 '+' 后缀、截断描述。"""
    return raw.strip().split("+", 1)[0].split(" ", 1)[0].upper() or "UNKNOWN"


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
    """解析 AllocTRES 的 GPU 已占，按节点型号归属。

    slurm 语义（实测贵大 22.05）：无型号的 gres/gpu=N 是该节点 GPU 已占
    **总量**（含未指名型号的申请）；带型号 gres/gpu:TYPE=M 只统计显式
    指名的那部分——两者并存时以总量为准，typed 用于归属，差额摊到节点
    型号上（单型号精确，多型号均摊）。
    """
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
    if not typeless and not typed:
        return {}
    total_alloc = typeless if typeless else sum(typed.values())
    out = dict(typed)
    remainder = max(0, total_alloc - sum(typed.values()))
    if remainder and gres_total:
        types = [t for t in gres_total if t not in out] or list(gres_total)
        base, rem = divmod(remainder, len(types))
        for t in types:
            out[t] = out.get(t, 0) + base
        for t in types[:rem]:
            out[t] += 1
    return out


RESOURCES_FILTERS = ("cpu_free", "cpu_gpu_free", "fully_idle")


async def resources(
    runner: Runner,
    *,
    partition: str | None = None,
    flt: str | None = None,
    gpu_only: bool = False,
    include_nodes: bool = False,
) -> dict[str, object]:
    """节点级 + 分区级的 CPU/GPU 余量，单次往返。

    - CPU 已占取 scontrol CPUAlloc；GPU 已占取 AllocTRES（gres/gpu 总量为准）
    - flt：cpu_free（有空闲 CPU）/ cpu_gpu_free（CPU 与 GPU 都有空闲）/
      fully_idle（CPU 与 GPU 全空闲）；gpu_only：只看有 GPU 的节点（可叠加）
    - partition：按分区过滤；过滤或 include_nodes 时返回节点明细，否则紧凑视图
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

    # 过滤：partition -> gpu_only -> 资源条件（cpu_free / cpu_gpu_free / fully_idle）
    if partition:
        want = partition.rstrip("*")
        rows = [r for r in rows if want in [p.rstrip("*") for p in r.partitions]]
    if gpu_only:
        rows = [r for r in rows if r.gpus]
    if flt == "cpu_free":
        rows = [r for r in rows if r.usable and r.cpu.free > 0]
    elif flt == "cpu_gpu_free":
        rows = [
            r
            for r in rows
            if r.usable and r.cpu.free > 0 and any(g.free > 0 for g in r.gpus.values())
        ]
    elif flt == "fully_idle":
        rows = [
            r
            for r in rows
            if r.usable and r.cpu.allocated == 0 and all(g.allocated == 0 for g in r.gpus.values())
        ]
    elif flt is not None:
        raise ValueError(f"未知 filter：{flt}（可选 {RESOURCES_FILTERS}）")

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
    if include_nodes or partition or gpu_only or flt:
        node_payload = [r.as_dict() for r in rows]
    return {
        "nodes": node_payload,
        "node_count": len(rows),
        "partitions": {k: v.as_dict() for k, v in sorted(agg.items())},
        "filters": {"partition": partition, "filter": flt, "gpu_only": gpu_only},
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


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "job"


def _script_has(script: str, *flags: str) -> bool:
    """用户脚本是否已含指定类别的 #SBATCH 指令。"""
    for line in script.splitlines():
        if not _SBATCH_LINE.match(line):
            continue
        body = line.strip().split(None, 1)[1] if len(line.strip().split(None, 1)) > 1 else ""
        for flag in flags:
            if body.split()[0].split("=")[0] == flag:
                return True
    return False


def build_sbatch_script(
    script: str,
    *,
    name: str,
    partition: str | None,
    nodes: int | None,
    cpus: int | None,
    gpus: int | None,
    time_limit: str | None,
    stdout_path: str,
    stderr_path: str,
) -> tuple[str, bool, bool]:
    """注入 #SBATCH 指令；用户脚本已声明同类指令则尊重之。

    返回 (最终脚本, stdout 由 clu 接管, stderr 由 clu 接管)。
    """
    inject: list[str] = []
    if name and not _script_has(script, "-J", "--job-name"):
        inject.append(f"#SBATCH -J {shlex.quote(name)}")
    if partition and not _script_has(script, "-p", "--partition"):
        inject.append(f"#SBATCH -p {shlex.quote(partition)}")
    if nodes and not _script_has(script, "-N", "--nodes"):
        inject.append(f"#SBATCH -N {int(nodes)}")
    if cpus and not _script_has(script, "-c", "--cpus-per-task"):
        inject.append(f"#SBATCH -c {int(cpus)}")
    if gpus and not _script_has(script, "--gres", "--gpus"):
        inject.append(f"#SBATCH --gres=gpu:{int(gpus)}")
    if time_limit and not _script_has(script, "-t", "--time"):
        inject.append(f"#SBATCH -t {shlex.quote(time_limit)}")
    own_out = _script_has(script, "-o", "--output")
    own_err = _script_has(script, "-e", "--error")
    if not own_out:
        inject.append(f"#SBATCH -o {stdout_path}")
    if not own_err:
        inject.append(f"#SBATCH -e {stderr_path}")
    lines = script.splitlines()
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    head, tail = lines[:insert_at], lines[insert_at:]
    final = "\n".join([*head, *inject, *tail])
    if not final.endswith("\n"):
        final += "\n"
    return final, not own_out, not own_err


async def submit(
    runner: Runner,
    script: str,
    *,
    label: str,
    name: str | None = None,
    partition: str | None = None,
    nodes: int | None = None,
    cpus: int | None = None,
    gpus: int | None = None,
    time_limit: str | None = None,
) -> dict[str, object]:
    """写脚本（base64 防注入）到 ~/.clu/jobs/<label>/ 后 sbatch。"""
    safe = _safe_name(name or "job")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    # sbatch 指令不展开 $HOME（实测贵大：StdOut=/home/u/$HOME/...）——必须用绝对路径
    home = (await _run(runner, 'printf "%s" "$HOME"')).stdout.strip()
    if not home.startswith("/"):
        home = "$HOME"  # 兜底（正常集群不会走到）
    base = f"{home}/.clu/jobs/{label}/{safe}-{stamp}"
    script_path = f"{base}.sh"
    stdout_path = f"{base}-%j.out"
    stderr_path = f"{base}-%j.err"
    final, clu_out, clu_err = build_sbatch_script(
        script,
        name=name or safe,
        partition=partition,
        nodes=nodes,
        cpus=cpus,
        gpus=gpus,
        time_limit=time_limit,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    encoded = base64.b64encode(final.encode("utf-8")).decode("ascii")
    command = (
        f"mkdir -p {shlex.quote(home)}/.clu/jobs/{shlex.quote(label)}"
        f" && echo {encoded} | base64 -d > {script_path}"
        f" && sbatch {script_path}"
    )
    result = await _run(runner, command)
    matched = _SBATCH_JOB_ID.search(result.stdout)
    if result.exit_code != 0 or matched is None:
        raise CluConnectionError(
            f"sbatch 失败（exit={result.exit_code}）：{result.stderr.strip()[:500]}"
        )
    job_id = matched.group(1)
    # %j 由 slurm 在作业启动时展开；解析出实际文件路径供直接读取
    return {
        "job_id": job_id,
        "script_path": script_path,
        "script": final,
        "stdout_path": stdout_path.replace("%j", job_id) if clu_out else None,
        "stderr_path": stderr_path.replace("%j", job_id) if clu_err else None,
        "name": name or safe,
        "partition": partition,
        "nodes": nodes,
        "cpus": cpus,
        "gpus": gpus,
    }


async def check_jobs(runner: Runner, job_ids: list[str]) -> dict[str, dict[str, object]]:
    """批量核对作业状态：squeue 取实时，已消失的用 sacct 补终态。"""
    if not job_ids:
        return {}
    ids = ",".join(shlex.quote(j) for j in job_ids)
    found: dict[str, dict[str, object]] = {}
    live = await _run(runner, f"squeue -h -j {ids} -o '%i|%T|%N|%r'")
    for line in live.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        found[parts[0]] = {
            "state": normalize_state(parts[1]),
            "node_list": parts[2] or None,
            "reason": parts[3] or None,
            "exit_code": None,
        }
    missing = [j for j in job_ids if j not in found]
    if missing:
        ids2 = ",".join(shlex.quote(j) for j in missing)
        acct = await _run(
            runner, f"sacct -n -P -j {ids2} -o JobID,State,ExitCode,NodeList 2>/dev/null || true"
        )
        for line in acct.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 4:
                continue
            base_id = parts[0].split(".", 1)[0]
            if base_id in found:
                continue  # 同一作业的 .batch/.extern 行：取首个
            found[base_id] = {
                "state": normalize_state(parts[1]),
                "node_list": parts[3] or None,
                "reason": None,
                "exit_code": parts[2] or None,
            }
    return found


async def cancel(runner: Runner, job_id: str) -> dict[str, object]:
    result = await _run(runner, f"scancel {shlex.quote(job_id)}")
    return {"job_id": job_id, "exit_code": result.exit_code, "stderr": result.stderr.strip()}
