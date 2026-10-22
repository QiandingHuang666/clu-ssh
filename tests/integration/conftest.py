"""集成测试夹具：Docker 容器当集群/服务器（DESIGN §10），不碰物理机与真实集群。

需要本机 docker；不可用时整组自动跳过（CI 的 macOS 节点即如此）。
"""

import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
IMAGE = "clu-it-sshd:latest"
POLL_TIMEOUT = 20.0


def _docker_available() -> bool:
    if sys.platform == "win32":  # windows runner 的 docker 仅 Linux 容器，sshd 镜像跑不了
        return False
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


def _wait_banner(
    read: Callable[[], bytes | None], what: str, timeout: float = POLL_TIMEOUT
) -> None:
    """读到 SSH banner 才算就绪（docker userland-proxy 会先接受 TCP，只测端口不可靠）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read():
            return
        time.sleep(0.2)
    raise RuntimeError(f"{what} {timeout}s 内未就绪")


def _banner_from_port(port: int) -> bytes | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.settimeout(2)
            data = sock.recv(64)
            return data if data.startswith(b"SSH-") else None
    except OSError:
        return None


def _banner_from_container(container: str) -> bytes | None:
    """容器自身 localhost 的 banner（用于未映射端口的目标容器）。"""
    try:
        out = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                "nc -w 2 127.0.0.1 22 </dev/null | head -c 4",
            ],
            capture_output=True,
            timeout=5,
        )
        return out.stdout if out.stdout.startswith(b"SSH-") else None
    except Exception:
        return None


def _wait_port(port: int, timeout: float = POLL_TIMEOUT) -> None:
    _wait_banner(lambda: _banner_from_port(port), f"容器端口 {port}", timeout)


def _host_port(name: str) -> int:
    out = subprocess.run(
        ["docker", "port", name, "22"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return int(out.rpartition(":")[2])


@pytest.fixture(scope="session")
def docker_ready() -> None:
    if not _docker_available():
        pytest.skip("本机无 docker，跳过容器化集成测试")


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    key = tmp_path_factory.mktemp("keys") / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


@pytest.fixture(scope="session")
def sshd_image(docker_ready: None) -> str:
    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, str(DOCKER_DIR)],
        check=True,
        capture_output=True,
    )
    return IMAGE


def _run_container(name: str, image: str, pubkey: str, extra: list[str]) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"PUBKEY={pubkey}",
            *extra,
            image,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def sshd(sshd_image: str, ssh_key: Path) -> dict[str, object]:
    """单台 sshd 服务器（随机映射到 127.0.0.1）。"""
    name = f"clu-it-sshd-{uuid.uuid4().hex[:8]}"
    pubkey = (ssh_key.with_suffix(".pub")).read_text(encoding="utf-8").strip()
    _run_container(name, sshd_image, pubkey, ["-p", "127.0.0.1::22"])
    port = _host_port(name)
    _wait_port(port)
    yield {"container": name, "host": "127.0.0.1", "port": port, "identity": str(ssh_key)}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="session")
def jump_net(sshd_image: str, ssh_key: Path) -> dict[str, object]:
    """跳板拓扑：target 不映射宿主端口，只能经 jump 的 docker 网络进入。"""
    net = f"clu-it-net-{uuid.uuid4().hex[:8]}"
    target = f"clu-it-target-{uuid.uuid4().hex[:8]}"
    jump = f"clu-it-jump-{uuid.uuid4().hex[:8]}"
    pubkey = (ssh_key.with_suffix(".pub")).read_text(encoding="utf-8").strip()
    try:
        subprocess.run(["docker", "network", "create", net], check=True, capture_output=True)
        _run_container(target, sshd_image, pubkey, ["--network", net])
        _run_container(jump, sshd_image, pubkey, ["--network", net, "-p", "127.0.0.1::22"])
        _wait_banner(lambda: _banner_from_container(target), f"target 容器 {target}")
        port = _host_port(jump)
        _wait_port(port)
        yield {"network": net, "target": target, "jump": {"container": jump, "port": port}}
    finally:
        for name in (target, jump):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
