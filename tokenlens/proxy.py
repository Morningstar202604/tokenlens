"""透明代理：把 /v1/* 请求转发到真实上游，顺手把 token 用量记下来。

用法（零侵入，改一行 base_url 即可）：
    OPENAI_BASE_URL=http://127.0.0.1:8787/v1

多上游：
    http://127.0.0.1:8787/v1/deepseek/chat/completions
    X-TokenLens-Upstream: https://api.moonshot.cn/v1
"""

from __future__ import annotations

import asyncio
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

# 请求体 / 缓冲式上游响应的内存上限：本地代理不需要无限流
MAX_BODY_BYTES = 50 * 1024 * 1024
MAX_BUFFER_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
_CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization",
                       "x-api-key", "api-key")


class ProxyContext:
    """一次转发过程中的共享状态。字段显式声明以便静态检查。"""

    def __init__(self, provider: str, upstream: str, model: Optional[str],
                 endpoint: str, project: str, key_hash: str, is_stream: bool,
                 request_id: str, started: float, req_bytes: int,
                 session_id: str = ""):
        self.provider = provider
        self.upstream = upstream
        self.model = model
        self.endpoint = endpoint
        self.project = project
        self.key_hash = key_hash
        self.is_stream = is_stream
        self.request_id = request_id
        self.started = started
        self.req_bytes = req_bytes
        self.session_id = session_id


def _split_alias(path: str, cfg: Config) -> tuple:
    """path 形如 'deepseek/chat/completions'，返回 (alias|None, 剩余路径)。"""
    first = path.split("/", 1)[0]
    if first in cfg.upstreams:
        rest = path.split("/", 1)[1] if "/" in path else ""
        return first, rest
    return None, path


def _client_headers(req: Request) -> Dict[str, str]:
    return {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}


# 内网 / 回环 / 链路本地等不可作为上游的地址段（IPv4 + IPv6）
_PRIVATE_NETS = (
    ("127.0.0.0/8", "loopback"), ("10.0.0.0/8", "private"), ("172.16.0.0/12", "private"),
    ("192.168.0.0/16", "private"), ("169.254.0.0/16", "link-local"), ("0.0.0.0/8", "unspecified"),
    ("::1/128", "loopback"), ("fc00::/7", "private"), ("fe80::/10", "link-local"),
    ("::ffff:0:0/96", "ipv4-mapped"), ("::/128", "unspecified"),
)
_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _ip_in_nets(ip: str) -> Optional[str]:
    """返回 ip 命中的保留网段名，未命中返回 None。"""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net, tag in _PRIVATE_NETS:
        if addr in ipaddress.ip_network(net):
            return tag
    return None


def _reject_unsafe_upstream(url: str, allow_private: bool) -> Optional[str]:
    """校验用户可控的上游地址（请求头/查询参数注入）。

    返回拒绝原因字符串；通过校验返回 None。
    """
    if not url or not url.strip():
        return "空地址"
    lowered = url.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return "仅支持 http/https 协议"
    from urllib.parse import urlparse
    try:
        host = urlparse(lowered).hostname
        port = urlparse(lowered).port
    except ValueError as exc:
        return f"URL 解析失败: {exc}"
    if not host:
        return "缺少主机名"
    host = host.rstrip(".").lower()
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return "禁止指向本机地址（localhost）"
    if allow_private:
        return None
    import ipaddress
    try:
        # 主机名本身是 IP：直接查保留段
        ipaddress.ip_address(host)
        tag = _ip_in_nets(host)
        if tag:
            return f"禁止指向{tag}网段地址（{host}）"
    except ValueError:
        pass
    # 域名：解析后检查是否落到保留段
    import socket
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return None  # 解析失败交由上游自然报错，不阻塞合法域名
    seen = set()
    for info in infos[:8]:
        ip = str(info[4][0])
        if ip in seen:
            continue
        seen.add(ip)
        if ":" in ip and not ip.startswith("::ffff:"):
            ip = ip.split("%")[0]
        tag = _ip_in_nets(ip)
        if tag:
            return f"域名 {host} 解析到{tag}网段地址（{ip}），已拒绝"
    return None


def register_proxy(app: FastAPI, cfg: Config, meter: Meter, client: httpx.AsyncClient):

    async def _forward(request: Request, path: str):
        started = time.time()
        rid = uuid.uuid4().hex[:12]
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": {"message": f"tokenlens: 请求体超过上限（{MAX_BODY_BYTES // (1024 * 1024)}MB）",
                           "type": "request_too_large"}},
                status_code=413, headers={"x-tokenlens-id": rid})

        # ---- 解析上游 ----
        alias, rest = _split_alias(path, cfg)
        override = request.headers.get("x-tokenlens-upstream") or request.query_params.get("upstream")
        if alias:
            base = cfg.upstreams[alias]
        elif override:
            reject = _reject_unsafe_upstream(override, cfg.allow_private_upstreams)
            if reject:
                # 安全拦截不落库：恶意探测请求没有统计价值，避免污染用量报表
                return JSONResponse(
                    {"error": {"message": f"tokenlens: 上游地址被拒绝：{reject}",
                               "type": "invalid_upstream"}},
                    status_code=400,
                    headers={"x-tokenlens-id": rid},
                )
            base = override
        else:
            base = cfg.default_upstream
        base = base.rstrip("/")
        target = f"{base}/{rest.lstrip('/')}" if rest else base
        if request.url.query:
            from urllib.parse import urlencode
            qs = request.query_params
            keep = {k: v for k, v in qs.items() if k != "upstream"}
            if keep:
                target += ("&" if "?" in target else "?") + urlencode(keep)

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
        session_id = request.headers.get("x-tokenlens-session", "")[:128]
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()
        else:
            api_key = auth.strip()
        if not api_key:
            # Anthropic 原生(x-api-key) / Azure(api-key) 风格：仅取指纹，不落明文
            api_key = (request.headers.get("x-api-key", "").strip()
                       or request.headers.get("api-key", "").strip())

        ctx = ProxyContext(
            provider=provider_from_url(base), upstream=base, model=model,
            endpoint=("/" + rest) if rest else "/", project=project,
            key_hash=key_hash(api_key), is_stream=is_stream, request_id=rid,
            started=started, req_bytes=len(body), session_id=session_id,
        )

        # 预算硬拦截：超限直接 402 拒绝，不转发上游
        if cfg.enforce_budget:
            reason = meter.enforce_check()
            if reason:
                await asyncio.to_thread(
                    meter.record,
                    provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                    endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                    is_stream=is_stream, status=402, error=reason,
                    latency_ms=(time.time() - started) * 1000,
                    req_bytes=ctx.req_bytes, request_id=rid,
                    session_id=ctx.session_id, estimated=True,
                )
                return JSONResponse(
                    {"error": {"message": f"tokenlens: {reason}",
                               "type": "budget_exceeded",
                               "scope": reason.split()[0]}},
                    status_code=402,
                    headers={"x-tokenlens-id": rid,
                             "x-tokenlens-scope": reason.split()[0]},
                )

        # 流式时尽量让上游回传 usage
        if is_stream and isinstance(payload, dict) and "messages" in payload and cfg.inject_stream_usage:
            payload.setdefault("stream_options", {})
            if isinstance(payload["stream_options"], dict):
                payload["stream_options"]["include_usage"] = True
            body = json.dumps(payload, ensure_ascii=False).encode()

        headers = _client_headers(request)
        headers.pop("x-tokenlens-upstream", None)

        # 逐跳跟随重定向：每一跳 Location 都重新过内网校验，防止公网上游
        # 302 跳板打进内网/云元数据（server.py 的 client 已关闭自动跟随）
        from urllib.parse import urljoin, urlparse
        method, current = request.method, target
        resp = None
        try:
            for hop in range(MAX_REDIRECTS + 1):
                upstream_req = client.build_request(method, current,
                                                    content=body if method != "GET" else None,
                                                    headers=headers)
                resp = await client.send(upstream_req, stream=is_stream)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                location = resp.headers.get("location")
                if not location or hop >= MAX_REDIRECTS:
                    break  # 重定向环/超次数：把最后一跳原样交回客户端
                next_url = urljoin(current, location)
                reject = _reject_unsafe_upstream(next_url, cfg.allow_private_upstreams)
                if reject:
                    await resp.aclose()
                    return JSONResponse(
                        {"error": {"message": f"tokenlens: 重定向目标被拒绝：{reject}",
                                   "type": "invalid_upstream"}},
                        status_code=400, headers={"x-tokenlens-id": rid})
                if urlparse(next_url).netloc != urlparse(current).netloc:
                    # 跨主机跳转不携带凭据（同浏览器语义），防 API key 外泄给跳转目标
                    for h in _CREDENTIAL_HEADERS:
                        headers.pop(h, None)
                if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                    method, body = "GET", b""
                current = next_url
                await resp.aclose()
        except Exception as exc:
            if resp is not None:
                await resp.aclose()
            await asyncio.to_thread(
                meter.record,
                provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                is_stream=is_stream, status=502, error=f"upstream error: {exc}",
                latency_ms=(time.time() - started) * 1000, req_bytes=ctx.req_bytes,
                request_id=rid, session_id=ctx.session_id, estimated=True,
            )
            return JSONResponse({"error": {"message": f"tokenlens: 上游连接失败 {exc}",
                                           "type": "upstream_error"}}, status_code=502)

        if resp is None:  # 循环至少发送一次，防御式收口（类型收窄）
            return JSONResponse({"error": {"message": "tokenlens: 上游无响应",
                                           "type": "upstream_error"}}, status_code=502)

        status = resp.status_code
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}
        resp_headers["x-tokenlens-id"] = rid

        if is_stream and status < 400:
            return await _stream_response(client, resp, ctx, status, resp_headers, started)
        return await _buffered_response(client, resp, ctx, status, resp_headers, started, payload)

    async def _buffered_response(client, resp, ctx, status, resp_headers, started, payload):
        raw = b""
        done = False
        truncated = False
        try:
            async for chunk in resp.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_BUFFER_BYTES:
                    # 无界读入会耗尽内存：超出上限即截断（响应头提示客户端）
                    truncated = True
                    break
            done = True
        except Exception as exc:
            raw = raw or b""
            resp_headers.pop("content-length", None)
            error = f"stream read error: {exc}"
        else:
            error = f"response truncated at {MAX_BUFFER_BYTES // (1024 * 1024)}MB" if truncated else None
            if truncated:
                resp_headers["x-tokenlens-truncated"] = "1"
                resp_headers.pop("content-length", None)
        finally:
            await resp.aclose()   # 共享连接池由 app 生命周期统一关闭，这里不关 client

        # 客户端提前断开时从开始到断开的时长会虚高，污染 avg/p95，中断场景不计入
        latency = (time.time() - started) * 1000 if done else 0
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

        await asyncio.to_thread(
            meter.record,
            provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
            endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
            is_stream=ctx.is_stream, status=status, error=error,
            latency_ms=latency, req_bytes=ctx.req_bytes, resp_bytes=len(raw),
            request_id=ctx.request_id, session_id=ctx.session_id,
            estimated=bool(usage.get("estimated")),
            **{k: v for k, v in usage.items() if k != "estimated"},
        )
        # 链路追踪：响应头回传本次成本与用量（流式无法预知，SSE 内自带 usage）
        if status < 400:
            cost = meter.calc.compute(ctx.model, usage.get("prompt_tokens") or 0,
                                      usage.get("completion_tokens") or 0,
                                      usage.get("cached_tokens") or 0,
                                      provider=ctx.provider)
            resp_headers["x-tokenlens-cost"] = f"{cost:.8f}"
            if usage:
                resp_headers["x-tokenlens-usage"] = json.dumps(
                    {k: v for k, v in usage.items() if k != "estimated"}, ensure_ascii=False)
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
            # 覆盖而非累加：OpenAI 兼容上游常在每条事件或末尾重复携带累计 usage，
            # 累加会把用量重复计费；以最后一次上报为准。
            for k, v in u.items():
                if v:
                    collected["usage"][k] = v
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
            done = False
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
                done = True
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
                await asyncio.to_thread(
                    meter.record,
                    provider=ctx.provider, upstream=ctx.upstream, model=ctx.model,
                    endpoint=ctx.endpoint, project=ctx.project, key_hash=ctx.key_hash,
                    is_stream=1, status=status, error=collected.get("error"),
                    # 流未完整转发（客户端断开）时延迟不计入，避免虚高污染统计
                    latency_ms=(time.time() - started) * 1000 if done else 0,
                    ttft_ms=collected.get("ttft"), req_bytes=ctx.req_bytes,
                    resp_bytes=collected["bytes"], request_id=ctx.request_id,
                    session_id=ctx.session_id,
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
