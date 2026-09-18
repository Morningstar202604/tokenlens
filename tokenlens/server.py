"""服务组装：代理 + 统计 API + 仪表盘静态页面。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import create_api
from .config import Config
from .meter import Meter
from .pricing import PricingTable
from .proxy import register_proxy
from .store import Store

WEB_DIR = Path(__file__).parent / "web"


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or Config.load()
    store = Store(cfg.db_path)
    pricing = PricingTable(cfg.pricing_overrides)
    meter = Meter(store, cfg, pricing)

    app = FastAPI(
        title="TokenLens",
        description="AI Token 用量监控代理与仪表盘",
        version="1.0.0",
    )
    app.state.cfg = cfg
    app.state.store = store
    app.state.meter = meter

    register_proxy(app, cfg, meter)
    app.include_router(create_api(store, meter))

    if (WEB_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/dashboard")
    def dashboard():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml") \
            if (WEB_DIR / "favicon.svg").exists() else None

    return app


def run(cfg: Optional[Config] = None, reload: bool = False):
    import uvicorn
    cfg = cfg or Config.load()
    app = create_app(cfg)
    print(_banner(cfg))
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


def _banner(cfg: Config) -> str:
    proxy = cfg.proxy_base
    lines = [
        "",
        "  TokenLens 已启动",
        f"  仪表盘    http://127.0.0.1:{cfg.port}",
        f"  代理地址  {proxy}          ← 把它填到 OPENAI_BASE_URL",
        f"  多上游    {proxy}/deepseek/chat/completions （别名见 ~/.tokenlens/config.json）",
        f"  数据库    {cfg.db_path}",
        f"  日预算    ${cfg.budget_daily:.2f}   月预算 ${cfg.budget_monthly:.2f}",
        "",
    ]
    return "\n".join(lines)
