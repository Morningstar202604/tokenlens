"""配置管理：持久化到 ~/.tokenlens/config.json，支持环境变量覆盖。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional

HOME = Path(os.environ.get("TOKENLENS_HOME", Path.home() / ".tokenlens"))
DEFAULT_CONFIG_PATH = HOME / "config.json"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787

    # 数据库文件（SQLite）
    db_path: str = str(HOME / "usage.db")

    # 上游别名 -> base url。请求 /v1/<alias>/chat/completions 会被转发到对应上游
    upstreams: Dict[str, str] = field(default_factory=lambda: {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "moonshot": "https://api.moonshot.cn/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "anthropic": "https://api.anthropic.com",
        "siliconflow": "https://api.siliconflow.cn/v1",
    })

    # 未指定别名时的默认上游
    default_upstream: str = "https://api.openai.com/v1"

    # 预算（USD）。0 表示不限制
    budget_daily: float = 10.0
    budget_monthly: float = 200.0

    # 触发告警的比例（0-1），达到预算的这个比例即告警
    alert_ratio: float = 0.8

    # 缓存命中的 prompt token 计费折扣（多数厂商 cached input 为 0.1 折或免费）
    cached_discount: float = 0.1

    # 按厂商覆盖缓存折扣：{"openai": 0.25}（OpenAI 缓存输入为 0.25 折）
    cached_discounts: Dict[str, float] = field(default_factory=lambda: {
        "openai": 0.25,
    })

    # 展示用汇率：USD -> CNY
    usd_cny_rate: float = 7.2

    # 自定义/覆盖价格：{"model": {"in": 1.0, "out": 4.0}} 单位 USD / 1M tokens
    pricing_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # 转发超时（秒）
    timeout: float = 120.0

    # 是否落库请求/响应原文（调试用，默认关闭以保护隐私）
    store_payloads: bool = False

    # 流式请求自动注入 stream_options.include_usage，让上游回传真实 usage
    inject_stream_usage: bool = True

    # 告警 webhook（可选）
    webhook_url: Optional[str] = None

    # 告警通道类型：generic | dingtalk | wecom | feishu（决定机器人卡片格式）
    webhook_type: str = "generic"

    # 仪表盘是否需要口令（留空则不鉴权）
    dashboard_token: Optional[str] = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                known = {f for f in cls.__dataclass_fields__}
                for k, v in data.items():
                    if k in known:
                        setattr(cfg, k, v)
            except Exception as exc:  # 配置损坏不应阻断启动
                print(f"[tokenlens] 配置文件读取失败({exc})，使用默认配置")
        # 环境变量覆盖常用项
        if os.environ.get("TOKENLENS_PORT"):
            cfg.port = int(os.environ["TOKENLENS_PORT"])
        if os.environ.get("TOKENLENS_UPSTREAM"):
            cfg.default_upstream = os.environ["TOKENLENS_UPSTREAM"]
        if os.environ.get("TOKENLENS_DAILY_BUDGET"):
            cfg.budget_daily = float(os.environ["TOKENLENS_DAILY_BUDGET"])
        cfg.db_path = os.path.expanduser(cfg.db_path)
        return cfg

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @property
    def proxy_base(self) -> str:
        return f"http://{self.host if self.host != '0.0.0.0' else '127.0.0.1'}:{self.port}/v1"
