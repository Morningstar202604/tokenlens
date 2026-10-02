"""托盘常驻单元测试：启动决策真值表 / 图标绘制 / 菜单回调 / 依赖缺失回退。

    python scripts/tray_test.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenlens.config import Config

CHECK = []


def check(name, cond, detail=""):
    CHECK.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def main():
    from tokenlens import tray

    print("[decide] 启动决策真值表")
    check("win32+独占+托盘依赖 → tray", tray.decide(True, True, "win32") == "tray")
    check("缺托盘依赖 → console 回退", tray.decide(True, False, "win32") == "console")
    check("终端共享控制台 → console", tray.decide(False, True, "win32") == "console")
    check("非 Windows → console", tray.decide(True, True, "linux") == "console")

    print("[tray_available]")
    ok = tray.tray_available()
    check("返回布尔且本环境为 True", isinstance(ok, bool) and ok is True)

    print("[_icon_image] 现场绘制不依赖资源文件")
    img = tray._icon_image()
    check("64x64 RGBA", img.size == (64, 64) and img.mode == "RGBA")

    print("[_handlers] 菜单回调")
    cfg = Config()
    fake = types.SimpleNamespace(should_exit=False)
    icon_stops = []
    fake_icon = types.SimpleNamespace(stop=lambda: icon_stops.append(1))
    opened = []
    real_open = tray.webbrowser.open
    tray.webbrowser.open = lambda url, *a, **kw: opened.append(url)
    try:
        h = tray._handlers(cfg, fake)
        h["open"]()
        check("打开仪表盘用本机地址", opened == ["http://127.0.0.1:8787"], str(opened))
        cfg2 = Config()
        cfg2.host = "0.0.0.0"
        cfg2.port = 9000
        h2 = tray._handlers(cfg2, fake)
        h2["open"]()
        check("0.0.0.0 监听时浏览器指向 127.0.0.1", opened[-1] == "http://127.0.0.1:9000", opened[-1])
        ret = h["quit"](fake_icon, None)
        check("退出置 should_exit + 显式停图标", fake.should_exit is True and ret is True and icon_stops == [1])
    finally:
        tray.webbrowser.open = real_open

    print("[_dashboard_url] 循环回环兜底")
    check("host 归一化", tray._dashboard_url(cfg) == "http://127.0.0.1:8787")

    print("[maybe_run_tray] 降级路径")
    real_avail, real_hide = tray.tray_available, tray.hide_console
    hidden = []
    tray.tray_available = lambda: False
    tray.hide_console = lambda: hidden.append(1)
    try:
        check("依赖缺失返回 False 不藏窗口", tray.maybe_run_tray(Config()) is False and not hidden)
    finally:
        tray.tray_available, tray.hide_console = real_avail, real_hide

    passed = sum(1 for _, c, _ in CHECK if c)
    print(f"\n{passed}/{len(CHECK)} PASS")
    return 0 if passed == len(CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
