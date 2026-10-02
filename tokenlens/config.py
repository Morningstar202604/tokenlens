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
        # 国外
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "xai": "https://api.x.ai/v1",
        "groq": "https://api.groq.com/openai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "together": "https://api.together.xyz/v1",
        "fireworks": "https://api.fireworks.ai/inference/v1",
        "perplexity": "https://api.perplexity.ai",
        "cerebras": "https://api.cerebras.ai/v1",
        "nvidia": "https://integrate.api.nvidia.com/v1",
        "deepinfra": "https://api.deepinfra.com/v1/openai",
        "cohere": "https://api.cohere.ai/compatibility/v1",
        "huggingface": "https://router.huggingface.co/v1",
        # 国内
        "deepseek": "https://api.deepseek.com/v1",
        "moonshot": "https://api.moonshot.cn/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "ark": "https://ark.cn-beijing.volces.com/api/v3",
        "qianfan": "https://qianfan.baidubce.com/v2",
        "hunyuan": "https://api.hunyuan.cloud.tencent.com/v1",
        "minimax": "https://api.minimax.chat/v1",
        "stepfun": "https://api.stepfun.com/v1",
        "baichuan": "https://api.baichuan-ai.com/v1",
        "yi": "https://api.lingyiwanwu.com/v1",
        "modelscope": "https://api-inference.modelscope.cn/v1",
        "siliconflow": "https://api.siliconflow.cn/v1",
        "spark": "https://spark-api-open.xf-yun.com/v1",
        "sensenova": "https://token.sensenova.cn/v1",
        "ai360": "https://api.360.cn/v1",
        # 本地推理运行时
        "ollama": "http://127.0.0.1:11434/v1",
        "lmstudio": "http://127.0.0.1:1234/v1",
        "vllm": "http://127.0.0.1:8000/v1",
        "llamacpp": "http://127.0.0.1:8080/v1",
        "jan": "http://127.0.0.1:1337/v1",
        "xinference": "http://127.0.0.1:9997/v1",
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

    # 流式请求自动注入 stream_options.include_usage，让上游回传真实 usage
    inject_stream_usage: bool = True

    # 告警 webhook（可选）
    webhook_url: Optional[str] = None

    # 告警通道类型：generic | dingtalk | wecom | feishu（决定机器人卡片格式）
    webhook_type: str = "generic"

    # 预算硬拦截：超限请求直接 402 拒绝，不再转发到上游
    enforce_budget: bool = True

    # 拦截阈值（占预算比例）：1.0 = 超支才拦，0.8 = 用到 80% 就拦
    enforce_budget_ratio: float = 1.0

    # 仪表盘是否需要口令（留空则不鉴权）
    dashboard_token: Optional[str] = None

    # 是否允许通过请求头/查询参数把上游指向内网地址（默认禁止，防 SSRF）
    allow_private_upstreams: bool = False

    # 数据保留天数（0 = 不清理）。prune 命令按此清理过期记录
    retention_days: int = 0

    # 密钥别名：{"sha256指纹前12位": "应用名"}，仪表盘按应用显示与统计
    key_aliases: Dict[str, str] = field(default_factory=dict)

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
