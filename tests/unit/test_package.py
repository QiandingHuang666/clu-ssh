"""包元数据一致性：版本单源、入口脚本、包名。"""

import tomllib
from pathlib import Path

import clu

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_is_single_sourced() -> None:
    # 版本唯一来源是 src/clu/__init__.py，pyproject 经 hatch 动态读取
    data = tomllib.loads(PYPROJECT.read_text())
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/clu/__init__.py"
    assert clu.__version__


def test_console_script_uses_cli_main() -> None:
    data = tomllib.loads(PYPROJECT.read_text())
    assert data["project"]["scripts"]["clu"] == "clu.cli:cli_main"


def test_package_name_is_clu_ssh() -> None:
    data = tomllib.loads(PYPROJECT.read_text())
    assert data["project"]["name"] == "clu-ssh"
