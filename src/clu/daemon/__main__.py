"""`python -m clu.daemon` 入口。"""

import asyncio
import contextlib

from clu.daemon.server import run_daemon

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_daemon())
