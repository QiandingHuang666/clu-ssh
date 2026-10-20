"""Slurm 命令层：squeue/sinfo/scontrol/sbatch 封装为结构化输出（DESIGN §6）。

Runner 注入（通常为 pool.run 的偏函数）；显式 -o 格式输出最可移植，
不依赖各集群差异较大的 --json 支持。脚本经 base64 上传避免引号注入。
"""

import base64
import re
import shlex
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

from clu.ssh.connection import CluConnectionError, ExecResult

Runner = Callable[
    ..., Awaitable[ExecResult]
]  # runner(command, *, timeout) — 通常为 pool.run 的偏函数

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

_SBATCH_JOB_ID = re.compile(r"Submitted batch job (\d+)")


async def _run(runner: Runner, command: str, *, timeout: float = 30.0) -> ExecResult:
    return await runner(command, timeout=timeout)


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


async def cluster_status(runner: Runner) -> dict[str, object]:
    """分区/节点/GPU 概览（sinfo）+ 队列状态计数（squeue）。"""
    info = await _run(runner, f"sinfo -h -o '{_PARTITION_FORMAT}'")
    partitions: list[dict[str, str]] = []
    for line in info.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != len(PARTITION_FIELDS):
            continue
        partitions.append(dict(zip(PARTITION_FIELDS, parts, strict=True)))
    queue = await _run(runner, "squeue -h -o '%T'")
    counts = Counter(line.strip() for line in queue.stdout.splitlines() if line.strip())
    return {"partitions": partitions, "queue": dict(counts)}


def _parse_scontrol(text: str) -> dict[str, str]:
    """scontrol show job 的 key=value 输出（值可能带空格，取首个 = 分段，best effort）。"""
    fields: dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if sep and key and key not in fields:
            fields[key] = value
    return fields


async def job_log(runner: Runner, job_id: str, *, tail: int = 100) -> dict[str, object]:
    """读作业 stdout：scontrol 找到 StdOut 路径后 tail。"""
    info = await _run(runner, f"scontrol show job {shlex.quote(job_id)}")
    fields = _parse_scontrol(info.stdout)
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
