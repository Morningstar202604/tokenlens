"""透明代理：把 /v1/* 请求转发到真实上游，顺手把 token 用量记下来。

用法（零侵入，改一行 base_url 即可）：
    OPENAI_BASE_URL=http://127.0.0.1:8787/v1

多上游：
    http://127.0.0.1:8787/v1/deepseek/chat/completions
    X-TokenLens-Upstream: https://api.moonshot.cn/v1
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .meter import Meter, key_hash, provider_from_url
from .tokenizer import count_request_prompt, count_text, extract_usage

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host", "accept-encoding",
}


class ProxyContext:
    """一次转发过程中的共享状态。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _split_alias(path: str, cfg: Config) -> tuple:
    """path 形如 'deepseek/chat/completions'，返回 (alias|None, 剩余路径)。"""
    first = path.split("/", 1)[0]
    if first in cfg.upstreams:
        rest = path.split("/", 1)[1] if "/" in path else ""
        return first, rest
    return None, path


def _client_headers(req: Request) -> Dict[str, str]:
    return {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}


def register_proxy(app: FastAPI, cfg: Config, meter: Meter, client: httpx.AsyncClient):

    async def _forward(request: Request, path: str):
        started = time.time()
        rid = uuid.uuid4().hex[:12]
        body = await request.body()

        # ---- 解析上游 ----
        alias, rest = _split_alias(path, cfg)
        override = request.headers.get("x-tokenlens-upstream") or request.query_params.get("upstream")
        if alias:
            base = cfg.upstreams[alias]
        elif override:
            base = override
        else:
            base = cfg.default_upstream
        base = base.rstrip("/")
        target = f"{base}/{rest.lstrip('/')}" if rest else base
        if request.url.query:
            qs = request.query_params
            keep = {k: v for k, v in qs.items() if k != "upstream"}
            if keep:
                target += ("&" if "?" in target else "?") + "&".join(f"{k}={v}" for k, v in keep.items())

        # ---- 解析请求体 ----
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        model = payload.get("model") if isinstance(payload, dict) else None
        is_stream = bool(isinstance(payload, dict) and payload.get("stream"))

        project = (request.headers.get("x-tokenlens-project")
                   or request.headers.get("x-title")
                   or request.headers.get("x-tokenlens-app")
                   or "default")
        auth = request.headers.get("authorization", "")
        api_key = auth.replace("Bearer ", "").strip() if auth.lower().startswith("bearer ") else auth

        ctx = ProxyContext(
            provider=provider_from_url(base), upstream=base, model=model,
            endpoint=("/" + rest) if rest else "/", project=project,
            key_hash=key_hash(api_key), is_stream=is_stream, request_id=rid,
            started=started, req_bytes=len(body),
        )

        # 预算硬拦截：超限直接 402 拒绝，不转发上游
        if cfg.enforce_budget:
            reason = meter.enforce_check()
            if reason:
                meter.record(
                    provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                    endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                    is_stream=is_stream, status=402, error=reason,
                    latency_ms=(time.time() - started) * 1000,
                    req_bytes=ctx.req_bytes, request_id=rid, estimated=True,
                )
                return JSONResponse(
                    {"error": {"message": f"tokenlens: {reason}",
                               "type": "budget_exceeded",
                               "scope": reason.split()[0]}},
                    status_code=402,
                )

        # 流式时尽量让上游回传 usage
        if is_stream and isinstance(payload, dict) and "messages" in payload and cfg.inject_stream_usage:
            payload.setdefault("stream_options", {})
            if isinstance(payload["stream_options"], dict):
                payload["stream_options"]["include_usage"] = True
            body = json.dumps(payload, ensure_ascii=False).encode()

        headers = _client_headers(request)
        headers.pop("x-tokenlens-upstream", None)

        req_args = dict(method=request.method, url=target, content=body, headers=headers)

        try:
            upstream_req = client.build_request(**req_args)
            resp = await client.send(upstream_req, stream=is_stream)
        except Exception as exc:
            meter.record(
                provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                is_stream=is_stream, status=502, error=f"upstream error: {exc}",
                latency_ms=(time.time() - started) * 1000, req_bytes=ctx.req_bytes,
                request_id=rid, estimated=True,
            )
            return JSONResponse({"error": {"message": f"tokenlens: 上游连接失败 {exc}",
                                           "type": "upstream_error"}}, status_code=502)

        status = resp.status_code
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}
        resp_headers["x-tokenlens-id"] = rid

        if is_stream and status < 400:
            return await _stream_response(client, resp, ctx, status, resp_headers, started)
        return await _buffered_response(client, resp, ctx, status, resp_headers, started, payload)

    async def _buffered_response(client, resp, ctx, status, resp_headers, started, payload):
        raw = b""
        try:
            async for chunk in resp.aiter_bytes():
                raw += chunk
        except Exception as exc:
            raw = raw or b""
            resp_headers.pop("content-length", None)
            error = f"stream read error: {exc}"
        else:
            error = None
        finally:
            await resp.aclose()   # 共享连接池由 app 生命周期统一关闭，这里不关 client

        latency = (time.time() - started) * 1000
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}

        usage = extract_usage(data) if isinstance(data, dict) else {}
        if not usage.get("prompt_tokens") and not usage.get("completion_tokens"):
            if status >= 400:
                # 出错的请求不计 token，但记错误
                pass
            elif isinstance(data, dict) and data.get("choices"):
                text = "".join(
                    (c.get("message", {}) or {}).get("content") or "" for c in data["choices"]
                )
                usage["completion_tokens"] = count_text(text, ctx.model)
                usage["prompt_tokens"] = count_request_prompt(payload, ctx.model)
                usage["estimated"] = True
        if status >= 400 and isinstance(data, dict):
            err = data.get("error")
            error = json.dumps(err, ensure_ascii=False)[:500] if err else (error or f"HTTP {status}")

        meter.record(
            provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
            endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
            is_stream=ctx.is_stream, status=status, error=error,
            latency_ms=latency, req_bytes=ctx.req_bytes, resp_bytes=len(raw),
            request_id=ctx.request_id, estimated=bool(usage.get("estimated")),
            **{k: v for k, v in usage.items() if k != "estimated"},
        )
        return Response(content=raw, status_code=status, headers=resp_headers,
                        media_type=resp.headers.get("content-type"))

    async def _stream_response(client, resp, ctx, status, resp_headers, started):
        resp_headers.pop("content-length", None)
        collected = {"text": [], "usage": {}, "ttft": None, "bytes": 0}
        buf = ""

        def handle_line(line: str):
            """逐行解析 SSE：data: 事件即 JSON，抓 usage 与增量文本。"""
            if not line.startswith("data:"):
                return
            data = line[5:].strip()
            if not data or data == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except Exception:
                return
            u = extract_usage(obj)
            for k, v in u.items():
                if v:
                    collected["usage"][k] = collected["usage"].get(k, 0) + v
            try:
                delta = obj["choices"][0]["delta"]
                if isinstance(delta, dict) and delta.get("content"):
                    collected["text"].append(delta["content"])
            except Exception:
                pass
            if obj.get("type") == "content_block_delta":
                d = (obj.get("delta") or {}).get("text")
                if d:
                    collected["text"].append(d)

        async def gen():
            nonlocal buf
            try:
                async for chunk in resp.aiter_bytes():
                    if collected["ttft"] is None and chunk:
                        collected["ttft"] = (time.time() - started) * 1000
                    collected["bytes"] += len(chunk)
                    yield chunk
                    # 按行缓冲解析：修复 SSE 事件跨 chunk 被切碎导致 usage 丢失的问题
                    buf += chunk.decode("utf-8", errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        handle_line(line)
            except Exception as exc:
                collected["error"] = f"stream aborted: {exc}"[:300]
            finally:
                if buf.strip():  # 流结束时处理末尾未换行的残留
                    for line in buf.split("\n"):
                        handle_line(line)
                await resp.aclose()
                usage = dict(collected["usage"])
                if not usage.get("completion_tokens"):
                    joined = "".join(collected["text"])
                    if joined:
                        usage["completion_tokens"] = count_text(joined, ctx.model)
                        usage["estimated"] = True
                if not usage.get("prompt_tokens") and usage.get("completion_tokens"):
                    usage["prompt_tokens"] = 0
                    usage["estimated"] = True
                meter.record(
                    provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                    endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                    is_stream=1, status=status, error=collected.get("error"),
                    latency_ms=(time.time() - started) * 1000,
                    ttft_ms=collected.get("ttft"), req_bytes=ctx.req_bytes,
                    resp_bytes=collected["bytes"], request_id=ctx.request_id,
                    estimated=bool(usage.pop("estimated", False)), **usage,
                )

        return StreamingResponse(gen(), status_code=status, headers=resp_headers,
                                 media_type=resp.headers.get("content-type") or "text/event-stream")

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_route(request: Request, path: str):
        return await _forward(request, path)

    @app.api_route("/v1", methods=["GET", "POST"])
    async def proxy_root(request: Request):
        return await _forward(request, "")
