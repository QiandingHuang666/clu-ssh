"""路径与运行时常量。

支持 CLU_CONFIG_DIR / CLU_DATA_DIR / CLU_RUNTIME_DIR 环境变量覆盖，
供集成测试做目录隔离。
"""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"  # 单点开关（测试可 monkeypatch）

APP_NAME = "clu"

CONNECTIONS_FILE = "connections.toml"
LEDGER_FILE = "ledger.sqlite3"
DAEMON_SOCKET = "daemon.sock"
DAEMON_LOG = "daemon.log"
KNOWN_HOSTS_FILE = "known_hosts"

# 连接行为（DESIGN §5.1 初始值，可经 pool 参数覆盖）
HEARTBEAT_INTERVAL = 30.0
HEARTBEAT_COUNT_MAX = 3
RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_MAX = 30.0
DEFAULT_CONNECT_TIMEOUT = 15.0

# 巡检
SESSION_CHECK_INTERVAL = 300.0  # 会话登记表定期核对（秒）
JOB_CHECK_INTERVAL = 60.0  # 作业台账定期核对（秒）

# 客户端 / daemon 拉起
CLIENT_TIMEOUT = 10.0
ENSURE_WAIT_TIMEOUT = 8.0


def _env_dir(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value).expanduser() if value else default


def config_dir() -> Path:
    """配置目录：POSIX ~/.config/clu/（XDG）；Windows %APPDATA%\\clu/。"""
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return _env_dir("CLU_CONFIG_DIR", base / APP_NAME)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return _env_dir("CLU_CONFIG_DIR", base / APP_NAME)


def data_dir() -> Path:
    """数据目录：POSIX ~/.local/share/clu/；Windows %LOCALAPPDATA%\\clu/。"""
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return _env_dir("CLU_DATA_DIR", base / APP_NAME)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return _env_dir("CLU_DATA_DIR", base / APP_NAME)


def runtime_dir() -> Path:
    """运行时目录：POSIX $XDG_RUNTIME_DIR 或 /tmp/clu-$UID；Windows %TEMP%\\clu/。"""
    if IS_WINDOWS:
        base = Path(os.environ.get("TEMP") or Path.home() / "AppData" / "Local" / "Temp")
        return _env_dir("CLU_RUNTIME_DIR", base / APP_NAME)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg else Path(f"/tmp/clu-{os.getuid()}")
    return _env_dir("CLU_RUNTIME_DIR", base / APP_NAME)


def daemon_port_file() -> Path:
    """Windows 传输：daemon.port（端口 + token，JSON）。"""
    return runtime_dir() / "daemon.port"


def connections_path() -> Path:
    return config_dir() / CONNECTIONS_FILE


def ledger_path() -> Path:
    return data_dir() / LEDGER_FILE


def daemon_socket() -> Path:
    return runtime_dir() / DAEMON_SOCKET


def daemon_log() -> Path:
    return runtime_dir() / DAEMON_LOG


def known_hosts_path() -> Path:
    return config_dir() / KNOWN_HOSTS_FILE
