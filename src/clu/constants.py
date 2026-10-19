"""路径与运行时常量。socket 与目录策略细节在 M1 固化。"""

import os
from pathlib import Path

APP_NAME = "clu"

CONNECTIONS_FILE = "connections.toml"
LEDGER_FILE = "ledger.sqlite3"
DAEMON_SOCKET = "daemon.sock"


def config_dir() -> Path:
    """配置目录：~/.config/clu/（遵循 XDG_CONFIG_HOME）。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def data_dir() -> Path:
    """数据目录：~/.local/share/clu/（留痕 SQLite）。"""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def runtime_dir() -> Path:
    """运行时目录：unix socket 所在（XDG_RUNTIME_DIR 优先）。"""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg else Path(f"/tmp/clu-{os.getuid()}")
    return base / APP_NAME
