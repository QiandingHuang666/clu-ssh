"""连接池集成测试：真实 asyncssh + 容器 sshd。"""

import pytest

from clu.config.store import ConnectionConfig
from clu.ssh.connection import CluConnectionError, KnownHosts, make_asyncssh_connector
from clu.ssh.pool import ConnectionPool

pytestmark = pytest.mark.integration


def make_pool(configs: dict[str, ConnectionConfig], tmp_path) -> ConnectionPool:
    known_hosts = KnownHosts(tmp_path / "known_hosts")
    return ConnectionPool(
        lambda: configs,
        make_asyncssh_connector(known_hosts),
        backoff_start=0.1,
        backoff_max=0.3,
    )


async def test_connect_and_run(sshd: dict, tmp_path) -> None:
    cfg = ConnectionConfig(
        label="t",
        kind="local",
        host=str(sshd["host"]),
        port=int(sshd["port"]),
        user="tester",
        identity=str(sshd["identity"]),
    )
    pool = make_pool({"t": cfg}, tmp_path)
    try:
        result = await pool.run("t", "echo hello-clu")
        assert result.exit_code == 0
        assert "hello-clu" in result.stdout
    finally:
        await pool.close()


async def test_known_hosts_recorded_then_tamper_rejected(sshd: dict, tmp_path) -> None:
    kh_path = tmp_path / "known_hosts"
    cfg = ConnectionConfig(
        label="t",
        kind="local",
        host=str(sshd["host"]),
        port=int(sshd["port"]),
        user="tester",
        identity=str(sshd["identity"]),
    )
    pool = make_pool({"t": cfg}, tmp_path)
    try:
        await pool.run("t", "true")
        assert "ssh-" in kh_path.read_text()
    finally:
        await pool.close()
    # 篡改 key 后必须拒绝（accept-new 语义的 strict 分支）
    from clu.ssh.connection import known_hosts_id

    kh_path.write_text(f"{known_hosts_id(cfg)} ssh-ed25519 AAAAFakeTamperedKey==\n")
    pool2 = make_pool({"t": cfg}, tmp_path)
    try:
        with pytest.raises(CluConnectionError):
            await pool2.run("t", "true")
    finally:
        await pool2.close()


async def test_reconnect_after_drop(sshd: dict, tmp_path) -> None:
    cfg = ConnectionConfig(
        label="t",
        kind="local",
        host=str(sshd["host"]),
        port=int(sshd["port"]),
        user="tester",
        identity=str(sshd["identity"]),
    )
    pool = make_pool({"t": cfg}, tmp_path)
    try:
        assert (await pool.run("t", "echo one")).exit_code == 0
        await pool.drop("t")
        assert (await pool.run("t", "echo two")).exit_code == 0
    finally:
        await pool.close()


async def test_jump_chain(jump_net: dict, ssh_key, tmp_path) -> None:
    configs = {
        "j": ConnectionConfig(
            label="j",
            kind="local",
            host="127.0.0.1",
            port=int(jump_net["jump"]["port"]),
            user="tester",
            identity=str(ssh_key),
        ),
        "tgt": ConnectionConfig(
            label="tgt",
            kind="instance",
            host=str(jump_net["target"]),
            user="tester",
            identity=str(ssh_key),
            proxy="j",
        ),
    }
    pool = make_pool(configs, tmp_path)
    try:
        result = await pool.run("tgt", "echo via-jump")
        assert result.exit_code == 0
        assert "via-jump" in result.stdout
    finally:
        await pool.close()
