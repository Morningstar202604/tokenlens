"""TokenLens.exe 入口：双击直接启动本地代理与仪表盘。

额外的命令行参数原样透传给 `tokenlens start`（如 --port 9000）。
"""

from __future__ import annotations

import sys

from tokenlens.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["start"] + sys.argv[1:]))
