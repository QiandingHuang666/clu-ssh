"""connections.toml 读写：tomlkit 保留注释，pydantic 校验。

结构（DESIGN §4）：

    [connections.<label>]
    kind = "cluster" | "instance" | "local"
    host / port / user / identity / proxy / profile

    [profiles.<name>]
    ...
"""

from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clu.profiles import Profile


class ConfigError(Exception):
    """配置文件损坏或内容非法。"""


class ConnectionConfig(BaseModel):
    """一条受管理的 SSH 连接。identity 为私钥路径；缺省走 ssh-agent 与默认 key。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: str = Field(default="instance", pattern=r"^(cluster|instance|local)$")
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    identity: str | None = None
    proxy: str | None = None
    profile: str | None = None


class ConfigData(BaseModel):
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    profiles: dict[str, Profile] = Field(default_factory=dict)


class ConfigStore:
    """基于 tomlkit 文档的就地编辑：未改动的条目（含注释）在重写后保持原样。

    外部改动按 mtime 失效重载——daemon 长驻进程必须能看到
    其他进程（clu connection import/add）写入的新连接。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._doc: tomlkit.TOMLDocument | None = None
        self._mtime: int | None = None

    # ---- 内部 ----
    def _mtime_now(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _document(self) -> tomlkit.TOMLDocument:
        mtime = self._mtime_now()
        if self._doc is None or mtime != self._mtime:
            if self._path.exists():
                try:
                    self._doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
                except tomlkit.exceptions.ParseError as exc:
                    raise ConfigError(f"{self._path} 不是合法 TOML：{exc}") from exc
            else:
                self._doc = tomlkit.document()
            self._mtime = mtime
        return self._doc

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(tomlkit.dumps(self._document()), encoding="utf-8")
        self._mtime = self._mtime_now()

    def _connection_table(self, label: str) -> tomlkit.TOMLDocument | Any:
        doc = self._document()
        conn_table = doc.setdefault("connections", tomlkit.table())
        if not hasattr(conn_table, "add"):  # connections 不是表（类型冲突）
            raise ConfigError("connections.toml: [connections] 应为表")
        if label in conn_table:
            raise ConfigError(f"连接 {label} 已存在（用 remove 后重新添加）")
        return conn_table

    # ---- 读取 ----
    def load(self) -> ConfigData:
        doc = self._document()
        connections: dict[str, ConnectionConfig] = {}
        profiles: dict[str, Profile] = {}
        conn_root = doc.get("connections")
        if conn_root is not None:
            for label, raw in dict(conn_root).items():
                try:
                    connections[label] = ConnectionConfig(label=label, **dict(raw))
                except ValidationError as exc:
                    raise ConfigError(f"连接 {label} 非法：{exc}") from exc
        prof_root = doc.get("profiles")
        if prof_root is not None:
            for name, raw in dict(prof_root).items():
                try:
                    profiles[name] = Profile(label=name, **dict(raw))
                except ValidationError as exc:
                    raise ConfigError(f"profile {name} 非法：{exc}") from exc
        # 引用完整性
        for cfg in connections.values():
            if cfg.proxy is not None and cfg.proxy not in connections:
                raise ConfigError(f"连接 {cfg.label} 的 proxy 指向不存在的连接 {cfg.proxy}")
            if cfg.profile is not None and cfg.profile not in profiles:
                raise ConfigError(f"连接 {cfg.label} 的 profile 不存在：{cfg.profile}")
        return ConfigData(connections=connections, profiles=profiles)

    # ---- 写入 ----
    def add_connection(self, cfg: ConnectionConfig, *, overwrite: bool = False) -> bool:
        """添加连接；label 已存在时 overwrite=False 则跳过。返回是否实际写入。"""
        doc = self._document()
        conn_table = doc.setdefault("connections", tomlkit.table())
        if cfg.label in conn_table:
            if not overwrite:
                return False
            del conn_table[cfg.label]
        table = tomlkit.table()
        table["kind"] = cfg.kind
        table["host"] = cfg.host
        table["port"] = cfg.port
        table["user"] = cfg.user
        if cfg.identity is not None:
            table["identity"] = cfg.identity
        if cfg.proxy is not None:
            table["proxy"] = cfg.proxy
        if cfg.profile is not None:
            table["profile"] = cfg.profile
        conn_table[cfg.label] = table
        self._flush()
        return True

    def remove_connection(self, label: str) -> bool:
        doc = self._document()
        conn_table = doc.get("connections")
        if conn_table is None or label not in conn_table:
            return False
        del conn_table[label]
        self._flush()
        return True
