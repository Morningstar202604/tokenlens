"""Token 计数。

优先级：
1. 上游响应里自带的 usage 字段（最准确，代理模式几乎总是走这条）
2. tiktoken 精确编码（OpenAI 系模型）
3. 启发式估算（中英混排加权 + 消息结构开销），保证离线/未知模型也能出数
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional

try:  # tiktoken 是可选依赖，缺失时静默降级
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None  # type: ignore

_CJK = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)

# 无法获取编码时的经验系数
_CJK_PER_CHAR = 0.75   # 中文约 1 token / 1~1.5 字
_OTHER_PER_CHAR = 1 / 3.8  # 英文约 4 字符 / token
_MESSAGE_OVERHEAD = 4  # 每条消息 role/分隔符开销
_REPLY_OVERHEAD = 3


@lru_cache(maxsize=8)
def _get_encoding(model: str):
    if tiktoken is None:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:  # 需要联网下载 BPE，失败则降级
            return None


@lru_cache(maxsize=8)
def _get_fallback_encoding():
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def heuristic_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk * _CJK_PER_CHAR + other * _OTHER_PER_CHAR))


def count_text(text: str, model: Optional[str] = None) -> int:
    if not text:
        return 0
    enc = _get_encoding(model or "") or _get_fallback_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return heuristic_tokens(text)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    parts.append("[image]")  # 图片按 ~1000 token 粗略计入
                    parts.append("x" * 3800)
                elif item.get("type") == "input_audio":
                    parts.append("x" * 3800)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def count_messages(messages: Iterable[Any], model: Optional[str] = None) -> int:
    """估算一次 chat 请求的 prompt token 数。"""
    total = 0
    n = 0
    for msg in messages or []:
        n += 1
        if isinstance(msg, dict):
            text = _content_to_text(msg.get("content"))
            if msg.get("tool_calls"):
                text += json_len(msg["tool_calls"])
            if msg.get("function_call"):
                text += json_len(msg["function_call"])
            if msg.get("name"):
                text += str(msg["name"])
        else:
            text = str(msg)
        total += count_text(text, model) + _MESSAGE_OVERHEAD
    if n:
        total += _REPLY_OVERHEAD
    return total


def json_len(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def count_request_prompt(payload: Dict[str, Any], model: Optional[str] = None) -> int:
    """对 OpenAI 兼容请求体估算输入 token。"""
    if not isinstance(payload, dict):
        return 0
    total = 0
    if payload.get("messages") is not None:
        total += count_messages(payload["messages"], model)
    elif payload.get("prompt") is not None:
        p = payload["prompt"]
        if isinstance(p, list):
            total += sum(count_text(_content_to_text(x), model) for x in p)
        else:
            total += count_text(_content_to_text(p), model)
    elif payload.get("input") is not None:  # Anthropic / Responses API
        inp = payload["input"]
        if isinstance(inp, str):
            total += count_text(inp, model)
        else:
            total += count_messages(inp, model)
    if payload.get("functions"):
        total += count_text(json_len(payload["functions"]), model)
    if payload.get("tools"):
        total += count_text(json_len(payload["tools"]), model)
    return total


def extract_usage(obj: Any) -> Dict[str, int]:
    """从各种风格的响应里尽力提取 usage。统一返回标准字段。"""
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
           "cached_tokens": 0, "reasoning_tokens": 0}
    if not isinstance(obj, dict):
        return out
    usage = obj.get("usage")
    if isinstance(usage, dict):
        pin = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        pout = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = usage.get("total_tokens")
        detail = usage.get("prompt_tokens_details") or {}
        cached = detail.get("cached_tokens") or 0
        if not cached:
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
        if not cached:
            cached = usage.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0
        cout_detail = usage.get("completion_tokens_details") or {}
        reasoning = cout_detail.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
        out.update({
            "prompt_tokens": int(pin or 0),
            "completion_tokens": int(pout or 0),
            "total_tokens": int(total or (int(pin or 0) + int(pout or 0))),
            "cached_tokens": int(cached or 0),
            "reasoning_tokens": int(reasoning or 0),
        })
    elif obj.get("type") == "message_start" and isinstance(obj.get("message"), dict):
        m = obj["message"]
        usage = m.get("usage") or {}
        out["prompt_tokens"] = int(usage.get("input_tokens") or 0)
    elif obj.get("type") == "message_delta":
        usage = obj.get("usage") or {}
        out["completion_tokens"] = int(usage.get("output_tokens") or 0)
    return out
