"""Slurm 命令层测试：解析与提交（注入 fake runner）。"""

import asyncio

import pytest

from clu.slurm import commands as slurm
from clu.ssh.connection import CluConnectionError, ExecResult


def make_runner(stdout: str = "", exit_code: int = 0) -> slurm.Runner:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        if command.startswith("printf"):
            return ExecResult(command, 0, "/home/tester", "", 1)
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
            sep = "---CLUSEP---"
            if command.startswith("sinfo"):
                return ExecResult(
                    command,
                    0,
                    f"gpu|up|4|idle|8/64|512G|gpu:A100:8\n{sep}\nRUNNING\nRUNNING\nPENDING\n",
                    "",
                    1,
                )
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
    result = await slurm.submit(runner, "#!/bin/bash\necho hi\n", label="gzu")
    assert result["job_id"] == "42"
    assert "gzu" in str(result["script_path"])
    assert str(result["script_path"]).endswith(".sh")
    assert "42.out" in str(result["stdout_path"])  # %j 已解析为 job id
    assert "42.err" in str(result["stderr_path"])


async def test_submit_failure_raises() -> None:
    runner = make_runner("some error\n", exit_code=1)
    with pytest.raises(CluConnectionError, match="sbatch 失败"):
        await slurm.submit(runner, "#!/bin/bash\n", label="gzu")


def test_build_sbatch_script_injects_directives() -> None:
    final, out, err = slurm.build_sbatch_script(
        "#!/bin/bash\necho hi\n",
        name="train",
        partition="gpu",
        nodes=1,
        cpus=8,
        gpus=2,
        time_limit="4:00:00",
        stdout_path="$HOME/.clu/jobs/g/j-%j.out",
        stderr_path="$HOME/.clu/jobs/g/j-%j.err",
    )
    lines = final.splitlines()
    assert lines[0] == "#!/bin/bash"  # 指令插在 shebang 之后
    joined = "\n".join(lines)
    for directive in (
        "#SBATCH -J train",
        "#SBATCH -p gpu",
        "#SBATCH -N 1",
        "#SBATCH -c 8",
        "#SBATCH --gres=gpu:2",
        "#SBATCH -t 4:00:00",
        "#SBATCH -o $HOME/.clu/jobs/g/j-%j.out",
        "#SBATCH -e $HOME/.clu/jobs/g/j-%j.err",
    ):
        assert directive in joined
    assert out is True and err is True


def test_build_sbatch_script_respects_user_directives() -> None:
    """用户脚本已声明同类指令时不覆盖（尤其 -o/-e：路径由用户负责）。"""
    script = "#!/bin/bash\n#SBATCH -p cpu\n#SBATCH -o my.out\nsqueue -u $USER\n"
    final, out, err = slurm.build_sbatch_script(
        script,
        name="x",
        partition="gpu",
        nodes=None,
        cpus=None,
        gpus=None,
        time_limit=None,
        stdout_path="$HOME/x-%j.out",
        stderr_path="$HOME/x-%j.err",
    )
    assert "#SBATCH -o my.out" in final
    assert "$HOME/x-%j.out" not in final
    assert "#SBATCH -p gpu" not in final  # 用户已声明 -p
    assert "#SBATCH -e $HOME/x-%j.err" in final  # 用户没声明 -e，注入
    assert out is False and err is True


def test_check_jobs_squeue_and_sacct() -> None:
    class StateRunner:
        async def __call__(self, command: str, *, timeout: float | None = None) -> ExecResult:
            if command.startswith("squeue"):
                return ExecResult(command, 0, "101|RUNNING|node1|None\n", "", 1)
            if command.startswith("sacct"):
                assert "102" in command
                return ExecResult(
                    command, 0, "102|COMPLETED|0:0|node2\n102.batch|COMPLETED|0:0|node2\n", "", 1
                )
            return ExecResult(command, 0, "", "", 1)

    found = asyncio.run(slurm.check_jobs(StateRunner(), ["101", "102"]))  # type: ignore[arg-type]
    assert found["101"]["state"] == "RUNNING"
    assert found["101"]["node_list"] == "node1"
    assert found["102"]["state"] == "COMPLETED"
    assert found["102"]["exit_code"] == "0:0"
    assert found["102"]["node_list"] == "node2"


def test_normalize_state() -> None:
    assert slurm.normalize_state("CANCELLED+") == "CANCELLED"
    assert slurm.normalize_state("CANCELLED by 1000") == "CANCELLED"
    assert slurm.normalize_state("running") == "RUNNING"
