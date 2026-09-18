"""TokenLens —— AI Token 用量监控工具。

两条接入路径：
  1) 代理模式（推荐，零侵入）：把 base_url 指向本地代理
  2) SDK 模式：装饰器 / monkeypatch 直接埋点
"""

from __future__ import annotations

__version__ = "1.0.0"

from .config import Config
from .meter import Meter
from .pricing import PricingTable
from .store import Store

__all__ = [
    "Config", "Store", "Meter", "PricingTable",
    "TokenLens", "track", "patch_openai", "configure", "get_default",
]


def get_default():
    from .sdk import get_default as _g
    return _g()


def configure(cfg=None, project: str = "default"):
    from .sdk import configure as _c
    return _c(cfg, project)


def track(project: str = "default", model=None, provider: str = "custom"):
    from .sdk import track as _t
    return _t(project, model, provider)


def patch_openai(project: str = "default"):
    from .sdk import patch_openai as _p
    return _p(project)


class TokenLens:  # 便捷别名
    def __init__(self, cfg=None, project: str = "default"):
        from .sdk import TokenLens as _T
        self._impl = _T(cfg, project)

    def __getattr__(self, name):
        return getattr(self._impl, name)
