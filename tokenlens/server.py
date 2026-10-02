"""服务组装：代理 + 统计 API + 仪表盘静态页面。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
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
    if cfg.retention_days > 0:
        # 启动即按保留天数清理，避免只靠手动 prune
        removed = store.prune(cfg.retention_days)
        if removed:
            print(f"[tokenlens] 启动清理：删除 {removed} 条 {cfg.retention_days} 天前的记录")
    pricing = PricingTable(cfg.pricing_overrides)
    meter = Meter(store, cfg, pricing)

    # 共享一个 httpx 连接池，避免每个请求重建连接（复用底层连接）
    # follow_redirects 必须关闭：重定向由 proxy 逐跳校验后手动跟随，
    # 否则公网上游可用 302 跳板绕过内网防护（SSRF）
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.timeout, connect=10.0),
        follow_redirects=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(
        title="TokenLens",
        description="AI Token 用量监控代理与仪表盘",
        version="1.2.1",
        lifespan=lifespan,
    )

    # 回环部署时校验 Host：DNS rebinding 域名解析到 127.0.0.1 但带恶意 Host，
    # 浏览器视为同源即可跨域读改仪表盘；对外监听（0.0.0.0）不做此限制
    if cfg.host in ("127.0.0.1", "localhost", "::1"):
        from fastapi.responses import JSONResponse

        @app.middleware("http")
        async def _host_guard(request, call_next):
            hosthdr = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
            if hosthdr and hosthdr not in ("127.0.0.1", "localhost", "::1"):
                return JSONResponse(
                    {"error": {"message": f"tokenlens: 非本机 Host 头（{hosthdr}）已拒绝，"
                                           "防止 DNS rebinding",
                               "type": "invalid_host"}},
                    status_code=400)
            return await call_next(request)
    app.state.cfg = cfg
    app.state.store = store
    app.state.meter = meter
    app.state.client = client

    register_proxy(app, cfg, meter, client)
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
    _warn_unsafe_deploy(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


def _warn_unsafe_deploy(cfg: Config):
    """启动前安全提示：对外监听未设 token / 上游配置指向内网。"""
    loopback = cfg.host in ("127.0.0.1", "localhost", "::1", "[::1]")
    if not loopback:
        if not cfg.dashboard_token:
            print("  ⚠ 警告: 正在对外监听（%s）但未设置 dashboard_token，"
                  "任何能访问该端口的人都能读取统计并看到明文 webhook 地址，"
                  "建议先配置访问令牌" % cfg.host)
        if not cfg.allow_private_upstreams:
            print("  ⚠ 提示: 上游地址校验已开启（防 SSRF）。"
                  "如需把请求转发到内网模型服务，请设置 allow_private_upstreams = true")
    from .proxy import _reject_unsafe_upstream
    for name, url in list(cfg.upstreams.items()) + [("default", cfg.default_upstream)]:
        reason = _reject_unsafe_upstream(url, cfg.allow_private_upstreams)
        if reason:
            print(f"  ⚠ 提示: 上游配置 {name}（{url}）{reason}；"
                  f"若确需使用内网模型服务，请设置 allow_private_upstreams = true")


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
