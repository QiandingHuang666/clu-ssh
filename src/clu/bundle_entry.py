"""PyInstaller 单二进制入口：按 argv[0] 分派 clu / clu-mcp。

安装脚本安装一个二进制文件，并创建 `clu-mcp` 软链（Windows 为副本）。
"""

import sys
from pathlib import Path


def main() -> None:
    name = Path(sys.argv[0]).name.lower()
    if name.startswith("clu-mcp"):
        from clu.mcp_server import main as mcp_main

        mcp_main()
    else:
        from clu.cli import app

        app()


if __name__ == "__main__":
    main()
