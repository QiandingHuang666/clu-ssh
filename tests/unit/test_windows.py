"""Windows 分支测试：目录常量与 TCP+token 传输（在 POSIX 上以开关模拟）。"""

from pathlib import Path

import pytest

import clu.constants as C
from clu import transport
from clu.client import CluClient, CluError


@pytest.fixture
def win_mode(monkeypatch, tmp_path: Path) -> Path:
    """模拟 Windows：开关 + APPDATA/LOCALAPPDATA/TEMP 指向临时目录。"""
    monkeypatch.setattr(C, "IS_WINDOWS", True)
    monkeypatch.setattr(transport, "IS_WINDOWS", True)
    appdata = tmp_path / "roaming"
    local = tmp_path / "local"
    temp = tmp_path / "temp"
    for d, env in ((appdata, "APPDATA"), (local, "LOCALAPPDATA"), (temp, "TEMP")):
        d.mkdir()
        monkeypatch.setenv(env, str(d))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return tmp_path


def test_windows_dirs(win_mode: Path) -> None:
    assert str(C.config_dir()).replace("\\", "/").endswith("roaming/clu")
    assert str(C.data_dir()).replace("\\", "/").endswith("local/clu")
    assert str(C.runtime_dir()).replace("\\", "/").endswith("temp/clu")
    assert C.daemon_port_file().name == "daemon.port"


async def test_windows_connect_without_daemon(win_mode: Path) -> None:
    """端口文件缺失 → 明确的 daemon_unreachable。"""
    client = CluClient(timeout=1.0)
    with pytest.raises(CluError) as exc:
        await client.request("ping")
    assert exc.value.code == "daemon_unreachable"
    assert "端口文件" in exc.value.message


async def test_windows_connect_bad_port_file(win_mode: Path) -> None:
    """端口文件损坏 → 同样归为 daemon_unreachable。"""
    pf = C.daemon_port_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not json", encoding="utf-8")
    client = CluClient(timeout=1.0)
    with pytest.raises(CluError) as exc:
        await client.request("ping")
    assert exc.value.code == "daemon_unreachable"


def test_posix_dirs_unchanged(monkeypatch, tmp_path: Path) -> None:
    """POSIX 分支不受 Windows 开关影响。"""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert str(C.config_dir()).endswith(".config/clu")
    assert str(C.data_dir()).endswith(".local/share/clu")
    assert str(C.runtime_dir()).startswith("/tmp/clu-")
