"""安全门测试：危险黑名单（无条件）与登录节点重操作拦截（DESIGN §8）。"""

import pytest

from clu.policy import check_exec, check_submit_script


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /tmp/x /",  # 尾部根
        "rm -rf ~",
        "rm -rf $HOME",
        "sudo rm -r -f /var",  # 分开的旗标
        "mkfs.ext4 /dev/sda1",
        "dd if=img.iso of=/dev/sda",
        "shutdown -h now",
        "reboot",
        "scancel --all",
        "scancel -u alice",
        ":(){ :|:& };:",
        "chmod -R 777 /",
        "sudo rm -rf /home",
        "rm -r -f /usr",
    ],
)
def test_dangerous_rejected_any_kind(command: str) -> None:
    for kind in ("cluster", "instance", "local"):
        decision = check_exec(command, kind=kind)
        assert not decision.allowed
        assert decision.rule == "dangerous"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf ./build",  # 相对路径允许
        "rm -rf /tmp/mydir",  # 深层绝对路径
        "rm -rf ~/data/exp1",  # 家目录子路径
        "rm -rf ./build ./dist",
        "echo hello",
        "squeue -u me",
        "cat slurm-123.out",
        "df -h",
        "scancel 12345",  # 单作业取消合法
    ],
)
def test_safe_allowed(command: str) -> None:
    assert check_exec(command, kind="cluster").allowed


@pytest.mark.parametrize(
    "command",
    [
        "torchrun train.py",
        "deepspeed --num_gpus 8 main.py",
        "accelerate launch run.py",
        "python -m torch.distributed.launch train.py",
        "make -j16",
        "make",
        "cmake --build build",
        "conda create -n env python=3.12",
        "conda install pytorch",
        "pip install transformers",
    ],
)
def test_heavy_rejected_on_cluster_login(command: str) -> None:
    decision = check_exec(command, kind="cluster")
    assert not decision.allowed
    assert decision.rule == "heavy"
    assert "submit" in decision.suggestion or "alloc" in decision.suggestion


def test_heavy_allowed_on_instance() -> None:
    """普通服务器（instance/local）没有登录节点概念，重操作放行。"""
    assert check_exec("torchrun train.py", kind="instance").allowed
    assert check_exec("pip install transformers", kind="local").allowed


def test_profile_extra_heavy_patterns() -> None:
    assert check_exec("run_big_bench.sh", kind="cluster").allowed
    decision = check_exec("run_big_bench.sh", kind="cluster", extra_heavy=[r"run_big_bench"])
    assert not decision.allowed and decision.rule == "heavy"


def test_submit_script_only_checks_dangerous() -> None:
    """脚本里跑重活是提交的本意，只拦危险命令。"""
    assert check_submit_script("#!/bin/bash\ntorchrun train.py").allowed
    assert not check_submit_script("#!/bin/bash\nrm -rf /").allowed


def test_heavy_guard_can_be_disabled() -> None:
    """profile heavy_guard=false 时放行重操作（危险黑名单仍生效）。"""
    assert not check_exec("torchrun train.py", kind="cluster").allowed
    assert check_exec("torchrun train.py", kind="cluster", heavy_guard=False).allowed
    assert not check_exec("rm -rf /", kind="cluster", heavy_guard=False).allowed
