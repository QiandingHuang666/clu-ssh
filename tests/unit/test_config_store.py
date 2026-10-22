"""配置存储测试：读写、注释保留、校验与引用完整性。"""

import pytest

from clu.config.store import ConfigError, ConfigStore, ConnectionConfig


def make_cfg(label: str = "a", **kw: object) -> ConnectionConfig:
    return ConnectionConfig(label=label, host="h1", user="u1", **kw)  # type: ignore[arg-type]


def test_missing_file_loads_empty(tmp_path: object) -> None:
    store = ConfigStore(tmp_path / "connections.toml")  # type: ignore[arg-type]
    data = store.load()
    assert data.connections == {}
    assert data.profiles == {}


def test_add_and_load_roundtrip(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    store = ConfigStore(path)  # type: ignore[arg-type]
    assert store.add_connection(make_cfg("gzu", kind="cluster", port=21563, proxy=None))
    assert not store.add_connection(make_cfg("gzu"))  # 已存在，跳过
    data = ConfigStore(path).load()  # type: ignore[arg-type]
    assert data.connections["gzu"].kind == "cluster"
    assert data.connections["gzu"].port == 21563


def test_comments_preserved_on_edit(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    path.write_text(  # type: ignore[arg-type]
        '# 我的连接\n[connections.old]\nhost = "h"\nport = 22\nuser = "u"\nkind = "instance"\n',
        encoding="utf-8",
    )
    store = ConfigStore(path)  # type: ignore[arg-type]
    store.add_connection(make_cfg("new"))
    content = path.read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert "# 我的连接" in content
    assert "[connections.new]" in content


def test_remove_connection(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    store = ConfigStore(path)  # type: ignore[arg-type]
    store.add_connection(make_cfg("a"))
    assert store.remove_connection("a")
    assert not store.remove_connection("a")
    assert ConfigStore(path).load().connections == {}  # type: ignore[arg-type]


def test_invalid_entry_raises(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    path.write_text(  # type: ignore[arg-type]
        '[connections.bad]\nhost = "h"\nuser = "u"\nkind = "wrong"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="bad"):
        ConfigStore(path).load()  # type: ignore[arg-type]


def test_dangling_proxy_raises(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    store = ConfigStore(path)  # type: ignore[arg-type]
    store.add_connection(make_cfg("a", proxy="ghost"))
    with pytest.raises(ConfigError, match="ghost"):
        store.load()


def test_corrupt_toml_raises(tmp_path: object) -> None:
    path = tmp_path / "connections.toml"
    path.write_text("not [ valid toml", encoding="utf-8")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="TOML"):
        ConfigStore(path).load()  # type: ignore[arg-type]


def test_reload_after_external_change(tmp_path: object) -> None:
    """daemon 场景：先加载（缓存），另一进程写入，本进程必须能看到。"""
    path = tmp_path / "connections.toml"  # type: ignore[arg-type]
    first = ConfigStore(path)  # type: ignore[arg-type]
    first.add_connection(make_cfg("a"))
    first.load()  # 缓存此刻的文档

    external = ConfigStore(path)  # type: ignore[arg-type]
    external.add_connection(make_cfg("b"))

    assert "b" in first.load().connections  # mtime 失效后重载
