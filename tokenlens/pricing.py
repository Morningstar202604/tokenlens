"""模型定价与成本计算。

内置一份主流厂商公开价格的快照（单位：USD / 1M tokens）。
价格随时可能变动，请以 `tokenlens pricing --edit` 或 pricing_overrides 覆盖为准。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

# 内置价格快照： {"匹配名": (input 单价, output 单价)}  USD / 1M tokens
BUILTIN_PRICING: Dict[str, Tuple[float, float]] = {
    # ---- OpenAI ----
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.4),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4-turbo": (10.0, 30.0),
    "o4-mini": (1.1, 4.4),
    "o3-mini": (1.1, 4.4),
    "o3": (2.0, 8.0),
    "o1-mini": (1.1, 4.4),
    "o1": (15.0, 60.0),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-ada-002": (0.10, 0.0),

    # ---- Anthropic ----
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4.1": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4.5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4.5": (1.0, 5.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),

    # ---- DeepSeek ----
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-chat": (0.27, 1.10),

    # ---- 阿里通义千问 ----
    "qwen3-max": (1.2, 6.0),
    "qwen-max": (1.6, 6.4),
    "qwen-plus": (0.4, 1.2),
    "qwen-turbo": (0.05, 0.2),
    "qwen-flash": (0.05, 0.5),
    "qwen2.5-72b": (0.35, 1.05),

    # ---- 字节豆包 / 火山方舟 ----
    "doubao-seed-1.6": (0.8, 8.0),
    "doubao-1.5-pro-32k": (0.8, 2.0),
    "doubao-pro-32k": (0.8, 2.0),
    "doubao-lite-32k": (0.3, 0.6),

    # ---- Moonshot Kimi ----
    "kimi-k2": (0.6, 2.5),
    "moonshot-v1-128k": (1.0, 1.0),
    "moonshot-v1-32k": (0.5, 0.5),
    "moonshot-v1-8k": (0.2, 0.2),

    # ---- 智谱 GLM ----
    "glm-4.6": (0.6, 2.2),
    "glm-4.5": (0.6, 2.2),
    "glm-4-plus": (0.7, 1.9),
    "glm-4-air": (0.1, 0.1),
    "glm-4-flash": (0.0, 0.0),

    # ---- Google Gemini ----
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),

    # ---- xAI ----
    "grok-4": (3.0, 15.0),
    "grok-3": (3.0, 15.0),

    # ---- 本地/自部署：免费 ----
    "ollama": (0.0, 0.0),
    "llama": (0.0, 0.0),
    "qwen-local": (0.0, 0.0),
}

# 常用后缀别名归一，命中后按主名查价
_ALIAS = {
    "gpt-5-mini-2025": "gpt-5-mini",
    "deepseek-v3": "deepseek-chat",
    "deepseek-r1": "deepseek-reasoner",
}


class PricingTable:
    """按模型名模糊匹配价格：长名优先，避免 gpt-4o-mini 被 gpt-4o 抢先命中。

    价格来源优先级：
    1. 用户 overrides（最高）
    2. pricing_data.json（由 scripts/sync_pricing.py 从 LiteLLM 同步，1000+ 模型）
    3. 内置 BUILTIN_PRICING（兜底，覆盖 LiteLLM 未收录的国内模型，如豆包/Kimi）
    """

    def __init__(self, overrides: Optional[Dict[str, Dict[str, float]]] = None,
                 data_path: Optional[str | Path] = None):
        self.table: Dict[str, Tuple[float, float]] = self._load_builtin(data_path)
        for name, p in (overrides or {}).items():
            self.table[name.lower()] = (float(p.get("in", 0.0)), float(p.get("out", 0.0)))
        self._keys = sorted(self.table.keys(), key=len, reverse=True)

    @staticmethod
    def _load_builtin(data_path: Optional[str | Path]) -> Dict[str, Tuple[float, float]]:
        table = dict(BUILTIN_PRICING)
        path = Path(data_path) if data_path else Path(__file__).parent / "pricing_data.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for k, v in data.get("models", {}).items():
                table[k] = (float(v.get("in", 0)), float(v.get("out", 0)))  # LiteLLM 数据更新鲜，覆盖内置
        except Exception:
            pass  # 数据文件缺失/损坏时回退内置表
        return table

    @classmethod
    def from_file(cls, path: str | Path) -> "PricingTable":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data.get("overrides", data))

    def lookup(self, model: Optional[str]) -> Optional[Tuple[float, float]]:
        if not model:
            return None
        m = model.lower().strip()
        if m in _ALIAS:
            m = _ALIAS[m]
        if m in self.table:
            return self.table[m]
        for key in self._keys:
            if key in m:
                return self.table[key]
        return None

    def price(self, model: Optional[str]) -> Tuple[float, float]:
        """返回 (input, output) USD/1M tokens；未知模型返回 0，避免污染成本统计。"""
        return self.lookup(model) or (0.0, 0.0)

    def known(self, model: Optional[str]) -> bool:
        return self.lookup(model) is not None

    def as_dict(self) -> Dict[str, Dict[str, float]]:
        return {k: {"in": v[0], "out": v[1]} for k, v in sorted(self.table.items())}


class CostCalculator:
    """把 token 数换算成金额。"""

    def __init__(self, pricing: PricingTable, cached_discount: float = 0.1,
                 cached_discounts: Optional[Dict[str, float]] = None):
        self.pricing = pricing
        self.cached_discount = cached_discount
        self.cached_discounts = cached_discounts or {}

    def compute(
        self,
        model: Optional[str],
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        provider: Optional[str] = None,
    ) -> float:
        pin, pout = self.pricing.price(model)
        cached = min(cached_tokens or 0, prompt_tokens or 0)
        billable_prompt = max((prompt_tokens or 0) - cached, 0)
        cost = (billable_prompt * pin + (completion_tokens or 0) * pout) / 1_000_000.0
        if cached:
            disc = self.cached_discounts.get((provider or "").lower(), self.cached_discount)
            cost += (cached * pin * disc) / 1_000_000.0
        return round(cost, 8)
