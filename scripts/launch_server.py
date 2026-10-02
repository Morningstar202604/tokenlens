"""TokenLens.exe 入口：双击直接启动本地代理与仪表盘。

支持完整子命令透传（TokenLens.exe onboard --auto / stats / doctor ...）；
不带子命令或带未知参数时按 `tokenlens start` 处理（如 --port 9000）。
"""

from __future__ import annotations

import sys

from tokenlens.__main__ import main

_SUBCOMMANDS = {
    "start", "stats", "top", "export", "import-csv", "pricing", "budget",
    "alerts", "seed-demo", "reset", "prune", "config", "live", "onboard",
    "doctor",
}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] in _SUBCOMMANDS:
        sys.exit(main(argv))
    sys.exit(main(["start"] + argv))
