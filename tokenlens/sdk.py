"""TokenLens SDK：不想走代理时，用装饰器或 monkeypatch 直接埋点。

    from tokenlens import track

    @track(project="my-bot")
    def ask(q):
        return client.chat.completions.create(model="gpt-4o-mini", messages=[...])

    # 或者一次性给整个 OpenAI SDK 打补丁（不改动任何业务代码）
    import tokenlens; tokenlens.patch_openai()
"""

from __future__ import annotations

import asyncio
import functools
import sys
import time
from typing import Any, Callable, Dict, Optional

from .config import Config
from .meter import Meter, key_hash, provider_from_url
from .store import Store
from .tokenizer import count_request_prompt, count_text, extract_usage

_default: Optional["TokenLens"] = None


class TokenLens:
    def __init__(self, cfg: Optional[Config] = None, project: str = "default"):
        self.cfg = cfg or Config.load()
        self.store = Store(self.cfg.db_path)
        self.meter = Meter(self.store, self.cfg)
        self.project = project

    def record_response(self, response: Any, *, model: Optional[str] = None,
                        project: Optional[str] = None, latency_ms: float = 0,
                        provider: str = "custom", request_payload: Optional[Dict] = None,
                        session_id: Optional[str] = None) -> Dict:
        """从任意 OpenAI/Anthropic 风格响应对象里抽取 usage 并落库。"""
        obj = _to_dict(response)
        usage = extract_usage(obj)
        model = model or obj.get("model") or (request_payload or {}).get("model")
        estimated = False
        if not usage.get("prompt_tokens") and request_payload:
            usage["prompt_tokens"] = count_request_prompt(request_payload, model)
            estimated = True
        if not usage.get("completion_tokens"):
            try:
                text = obj["choices"][0]["message"]["content"] or ""
            except Exception:
                text = ""
            if text:
                usage["completion_tokens"] = count_text(text, model)
                estimated = True
        usage.pop("total_tokens", None)
        return self.meter.record(
            provider=provider, model=model, endpoint="sdk",
            project=project or self.project, latency_ms=latency_ms,
            estimated=estimated, session_id=session_id, **usage,
        )


def get_default() -> TokenLens:
    global _default
    if _default is None:
        _default = TokenLens()
    return _default


def configure(cfg: Optional[Config] = None, project: str = "default") -> TokenLens:
    global _default
    _default = TokenLens(cfg, project)
    return _default


def _to_dict(obj: Any) -> Dict:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {}


def track(project: str = "default", model: Optional[str] = None,
          provider: str = "custom", session_id: Optional[str] = None) -> Callable:
    """装饰器：自动记录被包装函数的 LLM 调用用量。支持同步与异步函数。

    被包装函数抛异常时也会记录一条失败记录（status=500）再重新抛出。
    """

    def _record_failure(lens, started):
        try:
            lens.meter.record(
                provider=provider, model=model, endpoint="sdk",
                project=project, latency_ms=(time.time() - started) * 1000,
                status=500, error=f"sdk call failed: {sys.exc_info()[1]}"[:500],
                estimated=True, session_id=session_id,
            )
        except Exception as exc:
            print(f"[tokenlens] track 失败记录失败: {exc}")

    def _payload_from(args, kwargs):
        """从调用参数提取请求载荷：优先关键字参数；位置参数调用（如
        create(messages, ...)）时取第一个位置参数，避免漏记。"""
        payload = {}
        for k in ("messages", "input", "prompt", "content"):
            if kwargs.get(k) is not None:
                payload[k] = kwargs[k]
        if not any(k in payload for k in ("messages", "input", "prompt", "content")) and args:
            payload["messages"] = args[0]
        if kwargs.get("model") is not None:
            payload["model"] = kwargs["model"]
        return payload or None

    def deco(fn: Callable):
        lens = get_default()

        def finish(result, started, payload):
            latency = (time.time() - started) * 1000
            try:
                lens.record_response(result, model=model, project=project,
                                     latency_ms=latency, provider=provider,
                                     request_payload=payload, session_id=session_id)
            except Exception as exc:
                print(f"[tokenlens] track 失败: {exc}")
            return result

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                started = time.time()
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    _record_failure(lens, started)
                    raise
                return finish(result, started, _payload_from(args, kwargs))
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception:
                _record_failure(lens, started)
                raise
            return finish(result, started, _payload_from(args, kwargs))
        return wrapper

    return deco


def patch_openai(project: str = "default"):
    """给 openai SDK 的 chat.completions.create 打补丁，全程无需改动业务代码。"""
    try:
        from openai.resources.chat.completions import Completions
    except Exception as exc:
        raise RuntimeError(f"未检测到 openai SDK: {exc}")
    lens = get_default()

    for target, is_async in ((Completions, False),
                             (_try_import_async_completions(), True)):
        if target is None:
            continue
        original = getattr(target, "create", None)
        if original is None or getattr(original, "_tokenlens_patched", False):
            continue

        def make(orig, async_):
            def _record_failure(started):
                try:
                    lens.meter.record(
                        provider="openai", model=kwargs.get("model"), endpoint="sdk",
                        project=project, latency_ms=(time.time() - started) * 1000,
                        status=500, error=f"openai call failed: {sys.exc_info()[1]}"[:500],
                        estimated=True,
                    )
                except Exception as exc:
                    print(f"[tokenlens] patch 失败记录失败: {exc}")

            def wrapper(self, *args, **kwargs):
                started = time.time()
                try:
                    result = orig(self, *args, **kwargs)
                except Exception:
                    _record_failure(started)
                    raise
                try:
                    lens.record_response(
                        result, model=kwargs.get("model"), project=project,
                        latency_ms=(time.time() - started) * 1000,
                        provider="openai",
                        request_payload={"messages": kwargs.get("messages"),
                                         "model": kwargs.get("model")},
                    )
                except Exception as exc:
                    print(f"[tokenlens] patch 记录失败: {exc}")
                return result

            async def awrapper(self, *args, **kwargs):
                started = time.time()
                try:
                    result = await orig(self, *args, **kwargs)
                except Exception:
                    _record_failure(started)
                    raise
                try:
                    lens.record_response(
                        result, model=kwargs.get("model"), project=project,
                        latency_ms=(time.time() - started) * 1000,
                        provider="openai",
                        request_payload={"messages": kwargs.get("messages"),
                                         "model": kwargs.get("model")},
                    )
                except Exception as exc:
                    print(f"[tokenlens] patch 记录失败: {exc}")
                return result

            fn = awrapper if async_ else wrapper
            functools.update_wrapper(fn, orig)
            setattr(fn, "_tokenlens_patched", True)
            return fn

        target.create = make(original, is_async)
    print("[tokenlens] openai SDK 已接入监控")
    return lens


def _try_import_async_completions():
    try:
        from openai.resources.chat.completions import AsyncCompletions
        return AsyncCompletions
    except Exception:
        return None
