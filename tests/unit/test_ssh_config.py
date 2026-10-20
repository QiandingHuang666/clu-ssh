"""ssh config 导入测试：解析、跳过规则、ProxyJump 解析、kind 启发式、幂等导入。"""

from pathlib import Path

from clu.config.ssh_config import build_connections, import_into_store, parse_ssh_config
from clu.config.store import ConfigStore

SAMPLE = """
# 注释行
Host gzu
  HostName hpc.example.edu.cn
  Port 21563
  User alice
  IdentityFile ~/.ssh/id_ed25519

Host 4090
  HostName 127.0.0.1
  Port 6000
  User hqd

Host wsl
  hostname=192.168.8.131
  port 2222
  user hqd
  ProxyJump gl-r

Host gl-r
  HostName 192.168.8.1
  User root

Host *
  ServerAliveInterval 30

Host alias-only
  User u
"""


def test_parse_blocks() -> None:
    blocks = parse_ssh_config(SAMPLE)
    labels = [b["host"] for b in blocks]
    assert "gzu" in labels and "*" in labels


def test_build_connections_rules() -> None:
    configs, warnings = build_connections(parse_ssh_config(SAMPLE))
    by_label = {c.label: c for c in configs}
    assert "gzu" in by_label
    assert by_label["gzu"].port == 21563
    assert by_label["gzu"].identity is not None
    assert by_label["gzu"].kind == "instance"
    assert by_label["4090"].kind == "local"
    assert by_label["wsl"].port == 2222  # Key=value 形式
    assert by_label["wsl"].proxy == "gl-r"
    assert "*" not in by_label
    assert "alias-only" not in by_label
    assert any("通配" in w for w in warnings)
    assert any("HostName" in w for w in warnings)


def test_synthetic_jump_entry() -> None:
    text = "Host t\n  HostName 10.1.1.5\n  User u\n  ProxyJump relay@example.com:2222\n"
    configs, warnings = build_connections(parse_ssh_config(text))
    by_label = {c.label: c for c in configs}
    assert by_label["t"].proxy == "example.com-jump"
    assert by_label["example.com-jump"].user == "relay"
    assert by_label["example.com-jump"].port == 2222
    assert any("合成跳板" in w for w in warnings)


def test_import_into_store_idempotent(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "connections.toml")
    src = tmp_path / "sshconfig"
    src.write_text(SAMPLE, encoding="utf-8")
    report1 = import_into_store(store, src)
    assert "gzu" in report1.added
    report2 = import_into_store(store, src)
    assert "gzu" in report2.skipped
    assert report2.added == []
    data = store.load()
    assert data.connections["wsl"].proxy == "gl-r"
