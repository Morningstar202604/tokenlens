"""Mock 上游服务：模拟 OpenAI 兼容 API，用于本地自测与演示，不需要真实 API Key。

    python examples/mock_upstream.py --port 8901
"""

from __future__ import annotations

import argparse
import json
import random
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="TokenLens Mock Upstream")


def usage_for(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": int(prompt * 0.3)},
        "completion_tokens_details": {"reasoning_tokens": int(completion * 0.2)},
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "gpt-4o-mini")
    stream = bool(body.get("stream"))
    # 按请求文本长度伪造 token 数
    text = json.dumps(body.get("messages", []), ensure_ascii=False)
    prompt = max(8, len(text) // 3)
    completion = 120

    if body.get("__force_error"):
        return JSONResponse({"error": {"message": "mock upstream error",
                                       "type": "invalid_request_error"}}, status_code=400)

    if stream:
        chunk_size = int(body.get("__chunk_size") or 0)

        async def gen():
            words = ["你好", "这是", "一个", "模拟", "流式", "响应", "用于", "验证",
                     "TokenLens", "的", "计量", "能力", "。"]
            events = []
            for i, w in enumerate(words):
                chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}]}
                events.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
            if (body.get("stream_options") or {}).get("include_usage"):
                u = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                     "created": int(time.time()), "model": model, "choices": [],
                     "usage": usage_for(prompt, completion)}
                events.append(f"data: {json.dumps(u, ensure_ascii=False)}\n\n")
            else:
                # 模拟不回传 usage 的上游（此时 TokenLens 会退化为文本估算）
                events.append('data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
                              '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
            events.append("data: [DONE]\n\n")
            if chunk_size and chunk_size < len("".join(events)):
                # 模拟真实网络把 SSE 事件切碎到多个 TCP chunk，验证代理的行缓冲解析
                payload = "".join(events).encode()
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]
                    await _sleep(0.008)
            else:
                for ev in events:
                    yield ev.encode()
                    await _sleep(0.02)
        return StreamingResponse(gen(), media_type="text/event-stream")

    await _sleep(0.05)
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "这是模拟响应。"},
                     "finish_reason": "stop"}],
        "usage": usage_for(prompt, completion),
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    text = json.dumps(body.get("input", ""), ensure_ascii=False)
    return {
        "object": "list", "model": body.get("model", "text-embedding-3-small"),
        "data": [{"object": "embedding", "index": 0, "embedding": [0.01] * 8}],
        "usage": {"prompt_tokens": max(1, len(text) // 4), "total_tokens": max(1, len(text) // 4)},
    }


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model"},
                                       {"id": "deepseek-chat", "object": "model"}]}


async def _sleep(t: float):
    import asyncio
    await asyncio.sleep(t)


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"Mock upstream: http://{a.host}:{a.port}/v1")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
