"""转发管理器测试：未绑定/fake 连接拒绝、列表与移除逻辑。"""

import pytest

from clu.ssh.connection import CluConnectionError, ExecResult
from clu.ssh.forward import ForwardError, ForwardManager


def test_unbound_provider_raises() -> None:
    manager = ForwardManager()
    with pytest.raises(ForwardError, match="未绑定"):
        import asyncio

        asyncio.run(manager.add("a", "h", 22))


def test_fake_conn_rejected() -> None:
    class FakeConn:
        async def run(self, command: str, *, timeout: float | None = None) -> ExecResult:
            return ExecResult(command, 0, "", "", 0)

        def close(self) -> None:
            return None

    manager = ForwardManager()
    manager.bind(lambda label: _fake(FakeConn()))  # type: ignore[arg-type,return-value]
    import asyncio

    with pytest.raises(ForwardError, match="真实 SSH"):
        asyncio.run(manager.add("a", "h", 22))


async def _fake(conn: object) -> object:
    return conn


def test_list_and_remove_unknown() -> None:
    manager = ForwardManager()
    assert manager.list() == []
    assert manager.remove("fwd-99") is False


def test_clu_connection_error_passthrough() -> None:
    async def provider(label: str) -> object:
        raise CluConnectionError("未知连接：x")

    manager = ForwardManager()
    manager.bind(provider)  # type: ignore[arg-type]
    import asyncio

    with pytest.raises(CluConnectionError):
        asyncio.run(manager.add("x", "h", 22))
