"""~/.ssh/config 一次性导入：仅具体条目，解析 HostName/Port/User/ProxyJump/IdentityFile。

导入语义（DESIGN §4）：一次性副本，不跟踪源文件；跳过通配模式与纯别名条目；
kind 按 IP/主机名启发式推断（loopback/私网 -> local，其余 instance），
ProxyJump 指向已导入条目则建立 proxy 引用，否则合成跳板条目并告警。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from clu.config.store import ConfigStore, ConnectionConfig

_WILDCARDS = re.compile(r"[*?!]")
_LOCAL_HINTS = re.compile(r"^(localhost|127\.|::1|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_KV_LINE = re.compile(r"^(\S+?)\s*[= ]\s*(.+)$")


def parse_ssh_config(text: str) -> list[dict[str, str]]:
    """低层解析：返回每个 Host 块的参数字典（键小写，兼容 "Key value" 与 "Key=value"）。"""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        matched = _KV_LINE.match(line)
        if matched is None:
            continue
        key = matched.group(1).lower()
        value = matched.group(2).strip()
        if key == "host":
            current = {"host": value}
            blocks.append(current)
        elif current is not None:
            current[key] = value
    return blocks


def _looks_local(host: str) -> bool:
    return bool(_LOCAL_HINTS.match(host))


def _parse_jump_target(value: str) -> tuple[str, str | None, int | None]:
    """解析 ProxyJump 值：[user@]host[:port]。"""
    user: str | None = None
    host_part = value
    if "@" in value:
        user, _, host_part = value.partition("@")
    port: int | None = None
    if ":" in host_part:
        host_part, _, port_s = host_part.rpartition(":")
        if port_s.isdigit():
            port = int(port_s)
    return host_part, user or None, port


def build_connections(blocks: list[dict[str, str]]) -> tuple[list[ConnectionConfig], list[str]]:
    """把 Host 块转为连接配置；返回 (configs, warnings)。"""
    warnings: list[str] = []
    configs: list[ConnectionConfig] = []
    for block in blocks:
        tokens = block["host"].split()
        concrete = [t for t in tokens if not _WILDCARDS.search(t)]
        if not tokens or len(concrete) != len(tokens):
            if tokens:
                warnings.append(f"跳过含通配符的 Host 条目：{block['host']}")
            continue
        label = tokens[0]
        hostname = block.get("hostname")
        if not hostname:
            warnings.append(f"跳过无 HostName 的条目：{label}")
            continue
        port = int(block["port"]) if block.get("port", "").isdigit() else 22
        user = block.get("user")
        if not user:
            warnings.append(f"跳过无 User 的条目：{label}")
            continue
        configs.append(
            ConnectionConfig(
                label=label,
                kind="local" if _looks_local(hostname) else "instance",
                host=hostname,
                port=port,
                user=user,
                identity=block.get("identityfile"),
            )
        )
    # 解析 ProxyJump（两遍：先建条目，再连引用）
    by_label = {c.label: c for c in configs}
    labeled_blocks: dict[str, dict[str, str]] = {}
    for block in blocks:
        tokens = block["host"].split()
        if tokens and not any(_WILDCARDS.search(t) for t in tokens):
            labeled_blocks[tokens[0]] = block
    for cfg in configs:
        block = labeled_blocks.get(cfg.label, {})
        jump_raw = block.get("proxyjump")
        if not jump_raw:
            continue
        jump_host, jump_user, jump_port = _parse_jump_target(jump_raw)
        if jump_host in by_label:
            cfg.proxy = jump_host
        else:
            jump_label = f"{jump_host}-jump"
            synthetic = ConnectionConfig(
                label=jump_label,
                kind="instance",
                host=jump_host,
                port=jump_port or 22,
                user=jump_user or "root",
            )
            by_label[jump_label] = synthetic
            configs.append(synthetic)
            cfg.proxy = jump_label
            warnings.append(
                f"为 {cfg.label} 合成跳板条目 {jump_label}（来自 ProxyJump {jump_raw}）"
            )
    return configs, warnings


def import_into_store(
    store: ConfigStore,
    ssh_config_path: Path | None = None,
    *,
    overwrite: bool = False,
) -> ImportReport:
    """执行导入：写入 store，返回报告。已存在的 label 默认跳过。"""
    path = ssh_config_path or Path.home() / ".ssh" / "config"
    report = ImportReport()
    if not path.exists():
        report.warnings.append(f"未找到 {path}")
        return report
    existing = store.load().connections
    configs, warnings = build_connections(parse_ssh_config(path.read_text(encoding="utf-8")))
    report.warnings.extend(warnings)
    for cfg in configs:
        if cfg.label in existing and not overwrite:
            report.skipped.append(cfg.label)
            continue
        if store.add_connection(cfg, overwrite=overwrite):
            report.added.append(cfg.label)
            existing[cfg.label] = cfg
    return report
