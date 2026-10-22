"""pty-daemon（clu-ptyd）管理：随 wheel 分发的静态 Go 二进制的部署与调用。

远端零依赖：上传 → gunzip → chmod 即用。版本与 clu 对齐（构建期注入）。
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from clu.protocol import CluError


def _asset_dir() -> Path:
    """定位内嵌的 ptyd 资产目录（源码 / PyInstaller / Nuitka 三种形态）。"""
    candidates: list[Path] = []
    compiled = globals().get("__compiled__")  # Nuitka 注入
    if compiled is not None:
        base = Path(getattr(compiled, "containing_dir", "."))
        candidates += [base / "clu" / "ptyd", base / "ptyd"]
    if (meipass := getattr(sys, "_MEIPASS", None)) is not None:  # PyInstaller
        candidates.append(Path(meipass) / "clu" / "ptyd")
    candidates.append(Path(__file__).parent / "ptyd")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


ASSET_DIR = _asset_dir()
ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# clu-ptyd 独立版本（Go 改动时递增，触发远端重新部署；与 clu 的 __version__ 解耦）
PTYD_VERSION = "4"
SftpPut = Callable[[Path, str], Awaitable[None]]
Runner = Any  # runner(command, *, timeout) -> ExecResult


async def ensure_binary(runner: Runner, sftp_put: SftpPut, *, timeout: float = 30.0) -> str:
    """确保远端 ~/.clu/bin/clu-ptyd 就位且版本匹配，返回绝对路径。

    sftp_put(local_path, remote_path) 由调用方提供（经连接池 SFTP 通道）。
    """
    arch_out = await runner("uname -m")
    goarch = ARCH_MAP.get(arch_out.stdout.strip())
    if goarch is None:
        raise CluError("not_found", f"不支持的远端架构：{arch_out.stdout.strip()}")
    home = (await runner('printf "%s" "$HOME"')).stdout.strip()
    if not home.startswith("/"):
        raise CluError("internal", "无法解析远端家目录")
    bin_path = f"{home}/.clu/bin/clu-ptyd"
    chk = await runner(f"{bin_path} version 2>/dev/null || true")
    if chk.exit_code == 0 and PTYD_VERSION in chk.stdout:
        return bin_path
    local = ASSET_DIR / f"clu-ptyd-linux-{goarch}.gz"
    if not local.exists():
        raise CluError("not_found", f"ptyd 资产缺失：{local.name}（发布包应内含）")
    # 先建目录再上传（SFTP put 不会自动创建父目录）
    await runner(f"mkdir -p $(dirname {shlex.quote(bin_path)})")
    await sftp_put(local, f"{bin_path}.gz")
    # 运行中的二进制不可直接覆盖（Text file busy）：停旧守护 → 临时文件 → 原子 mv
    r = await runner(
        f"mkdir -p $(dirname {shlex.quote(bin_path)})"
        f" && {bin_path} stop 2>/dev/null || true"
        f" && gunzip -cf {bin_path}.gz > {bin_path}.new && chmod 700 {bin_path}.new"
        f" && mv -f {bin_path}.new {bin_path} && rm -f {bin_path}.gz"
        f" && {bin_path} version"
    )
    if r.exit_code != 0 or PTYD_VERSION not in r.stdout:
        raise CluError("internal", f"ptyd 部署失败：{(r.stderr or r.stdout).strip()[:200]}")
    return bin_path


async def ensure_running(runner: Runner, bin_path: str) -> None:
    """守护不在运行则以 setsid 拉起（独立于任何 ssh 会话存活）。"""
    r = await runner(f"{bin_path} list >/dev/null 2>&1")
    if r.exit_code == 0:
        return  # 已在运行
    await runner(f"setsid {shlex.quote(bin_path)} serve >/dev/null 2>&1 < /dev/null &")
    for _ in range(20):  # 最多等 2s 就绪
        probe = await runner(f"{bin_path} list >/dev/null 2>&1")
        if probe.exit_code == 0:
            return
        import asyncio

        await asyncio.sleep(0.1)
    raise CluError("internal", "ptyd 守护拉起失败（见远端 ~/.clu/ptyd 日志）")
