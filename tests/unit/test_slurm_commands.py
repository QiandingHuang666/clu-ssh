"""Slurm 命令层测试：解析与提交（注入 fake runner）。"""

import pytest

from clu.slurm import commands as slurm
from clu.ssh.connection import CluConnectionError, ExecResult


def make_runner(stdout: str = "", exit_code: int = 0) -> slurm.Runner:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        return ExecResult(
            command=command, exit_code=exit_code, stdout=stdout, stderr="", duration_ms=5
        )

    return run  # type: ignore[return-value]


async def test_list_jobs_parse_and_filter() -> None:
    stdout = (
        "12345|RUNNING|train|gpu|01:00|2:00:00|1|8|64G|gpu:A100:2|None\n"
        "12346|PENDING|eval|cpu|0:00|1:00|1|2|8G|(null)|Resources\n"
    )
    jobs = await slurm.list_jobs(make_runner(stdout), "u1")
    assert len(jobs) == 2
    assert jobs[0]["job_id"] == "12345"
    assert jobs[0]["gres"] == "gpu:A100:2"
    running = await slurm.list_jobs(make_runner(stdout), "u1", states=["RUNNING"])
    assert [j["job_id"] for j in running] == ["12345"]


async def test_cluster_status_counts() -> None:
    class SwitchRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            self.calls.append(command)
            if command.startswith("sinfo"):
                return ExecResult(command, 0, "gpu|up|4|idle|8/64|512G|gpu:A100:8\n", "", 1)
            return ExecResult(command, 0, "RUNNING\nRUNNING\nPENDING\n", "", 1)

    status = await slurm.cluster_status(SwitchRunner())  # type: ignore[arg-type]
    assert status["partitions"][0]["partition"] == "gpu"
    assert status["queue"] == {"RUNNING": 2, "PENDING": 1}


async def test_job_log_via_scontrol() -> None:
    class LogRunner:
        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            if command.startswith("scontrol"):
                return ExecResult(
                    command,
                    0,
                    "JobId=12345 JobName=x JobState=RUNNING StdOut=/home/u/slurm-12345.out\n",
                    "",
                    1,
                )
            return ExecResult(command, 0, "line1\nline2\nCLUEOF0\n", "", 1)

    log = await slurm.job_log(LogRunner(), "12345")  # type: ignore[arg-type]
    assert log["stdout_path"] == "/home/u/slurm-12345.out"
    assert log["exists"] is True
    assert "line2" in str(log["text"])


async def test_submit_parses_job_id() -> None:
    runner = make_runner("Submitted batch job 42\n")
    result = await slurm.submit(runner, "#!/bin/bash\necho hi\n")
    assert result["job_id"] == "42"
    assert ".clu/jobs" in str(result["script_path"])


async def test_submit_failure_raises() -> None:
    runner = make_runner("some error\n", exit_code=1)
    with pytest.raises(CluConnectionError, match="sbatch 失败"):
        await slurm.submit(runner, "#!/bin/bash\n")
