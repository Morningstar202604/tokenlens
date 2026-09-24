#!/usr/bin/env python3
"""从 LiteLLM 同步模型价格表到 tokenlens/pricing_data.json。

数据来源：BerriAI/litellm 的 model_prices_and_context_window.json（MIT 许可）。
用法：
    python scripts/sync_pricing.py                # 默认写到 tokenlens/pricing_data.json
    python scripts/sync_pricing.py --out <path>   # 自定义输出路径
"""

from __future__ import annotations

import argparse
import json
import urllib.request

SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DEFAULT_OUT = "tokenlens/pricing_data.json"

# 模板占位模型名（带通配/平台前缀，无法对应真实请求模型）
TEMPLATE_MARKERS = ("*", "azure/", "bedrock/", "vertex_ai/", "bria/", "sagemaker/", "watsonx/")


def fetch() -> dict:
    print(f"下载 {SOURCE} ...")
    with urllib.request.urlopen(SOURCE, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def convert(data: dict) -> dict:
    table: dict = {}
    skipped: list[str] = []
    for model, v in data.items():
        if not isinstance(v, dict):
            continue
        if any(m in model for m in TEMPLATE_MARKERS):
            skipped.append(model)
            continue
        pin = v.get("input_cost_per_token")
        pout = v.get("output_cost_per_token")
        if pin is None and pout is None:
            skipped.append(model)
            continue
        entry = {
            "in": round(float(pin or 0) * 1_000_000, 6),
            "out": round(float(pout or 0) * 1_000_000, 6),
        }
        # 无前缀 key 直接收录；带 provider 前缀（如 openai/gpt-4o）时收录去前缀别名，
        # 且仅在别名未被收录时写入，避免互相覆盖
        if "/" not in model:
            table.setdefault(model, entry)
        else:
            base = model.split("/", 1)[1]
            if base and base not in table and not any(m in base for m in TEMPLATE_MARKERS):
                table[base] = entry
    return table


def main() -> None:
    ap = argparse.ArgumentParser(description="同步 LiteLLM 模型价格表到 TokenLens")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    raw = fetch()
    table = convert(raw)
    payload = {
        "source": SOURCE,
        "note": "单位 USD/1M tokens，由 LiteLLM model_prices_and_context_window.json 转换，可用 sync_pricing.py 重新同步",
        "models": table,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"已写入 {args.out}: {len(table)} 个模型（原始 {len(raw)} 项）")


if __name__ == "__main__":
    main()
