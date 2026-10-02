"""Windows 托盘常驻：双击 exe 启动时隐藏独占控制台，托盘图标提供「打开仪表盘 / 退出」。

依赖可选（`pip install tokenlens[tray]`）：pystray + pillow。
非 Windows / 依赖缺失 / 终端共享控制台 / 服务起不来时一律回退常规控制台模式，
CLI 与服务器行为不变；绝不在拿不准的情况下隐藏任何窗口。
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict

from .config import Config


def decide(console_exclusive: bool, tray_ok: bool, platform: str) -> str:
    """启动模式决策：'tray' 仅当 Windows + 控制台独占（双击启动）+ 托盘依赖齐备。"""
    return "tray" if platform == "win32" and console_exclusive and tray_ok else "console"


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:  # aqg: top-level boundary 可选依赖缺失是正常路径，非错误
        return False


def console_exclusive() -> bool:
    """控制台是否只挂着本程序自己的进程（双击启动的 PyInstaller one-file：
    bootloader 与子进程同名同控制台）。终端（cmd/PowerShell/python）共享控制台时
    必有外来进程，判 False；枚举失败或无控制台也判 False（fail-closed → 控制台模式）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        buf = (ctypes.c_uint32 * 64)()
        n = k32.GetConsoleProcessList(buf, 64)
        if n <= 0 or n > 64:
            return False
        me = Path(sys.executable).name.lower()
        for i in range(n):
            h = k32.OpenProcess(0x1000, False, buf[i])  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                size = ctypes.c_uint32(1024)
                img = ctypes.create_unicode_buffer(1024)
                if not k32.QueryFullProcessImageNameW(h, 0, img, ctypes.byref(size)):
                    return False
            finally:
                k32.CloseHandle(h)
            if Path(img.value).name.lower() != me:
                return False
        return True
    except Exception:  # aqg: top-level boundary 判不了就回退控制台模式
        return False


def hide_console() -> None:
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:  # aqg: top-level boundary 藏不住就留着，不影响功能
        pass


def show_console() -> None:
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    except Exception:  # aqg: top-level boundary
        pass


def _dashboard_url(cfg: Config) -> str:
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "::1") else cfg.host
    return f"http://{host}:{cfg.port}"


def _icon_image():
    """托盘图标现场绘制：深色圆角底 + 青色透镜环，不依赖任何资源文件。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(15, 23, 42, 255))
    d.ellipse((15, 15, 49, 49), outline=(45, 212, 191, 255), width=6)
    d.ellipse((27, 27, 37, 37), fill=(45, 212, 191, 255))
    return img


def _handlers(cfg: Config, server) -> Dict[str, object]:
    url = _dashboard_url(cfg)

    def open_dashboard(icon=None, item=None):
        webbrowser.open(url)
        return False

    def quit_app(icon=None, item=None):
        server.should_exit = True
        if icon is not None:
            icon.stop()  # 显式停循环：不依赖"返回 True 即停"的隐式约定
        return True

    return {"open": open_dashboard, "quit": quit_app}


def run_tray(cfg: Config) -> bool:
    """托盘常驻：预检 → 藏控制台 → 服务器线程 + 托盘循环。
    用户点「退出」后返回 True；依赖缺失 / 预检失败 / 服务起不来返回 False，
    调用方回退控制台模式。"""
    if not tray_available():
        return False
    from .server import _preflight, create_app
    try:
        _preflight(cfg)
    except SystemExit:
        return False  # 端口占用 / 目录不可写：保留控制台让既有友好报错可见
    hide_console()

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        show_console()  # 罕见失败：把窗口还回去，回控制台模式让 uvicorn 报错可见
        return False

    import pystray
    handlers = _handlers(cfg, server)
    icon = pystray.Icon(
        "TokenLens", _icon_image(), "TokenLens · AI 花销账本",
        pystray.Menu(
            pystray.MenuItem("打开仪表盘", handlers["open"], default=True),
            pystray.MenuItem("退出", handlers["quit"]),
        ),
    )
    icon.run()
    thread.join(timeout=10.0)
    return True


def maybe_run_tray(cfg: Config) -> bool:
    """双击启动入口：接管条件全满足才进托盘，否则返回 False 走原控制台路径。"""
    if decide(console_exclusive(), tray_available(), sys.platform) != "tray":
        return False
    return run_tray(cfg)
