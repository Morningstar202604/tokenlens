"""命令行入口：python -m tokenlens <command>"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from .api import parse_range
from .config import Config, DEFAULT_CONFIG_PATH
from .meter import Meter
from .pricing import PricingTable
from .store import Store


def _ctx(cfg: Config):
    store = Store(cfg.db_path)
    meter = Meter(store, cfg)
    return store, meter


def _table(headers, rows, aligns=None):
    cols = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r[:cols]):
            widths[i] = max(widths[i], len(str(c)))
    aligns = aligns or ["l"] * cols

    def line(ch="-"):
        return "+" + "+".join(ch * (w + 2) for w in widths) + "+"

    def row(cells):
        out = []
        for i, c in enumerate(cells):
            s = str(c)
            out.append((" " + s.ljust(widths[i]) + " ") if aligns[i] != "r"
                       else (" " + s.rjust(widths[i]) + " "))
        return "|" + "|".join(out) + "|"

    print(line())
    print(row(headers))
    print(line("="))
    for r in rows:
        print(row(list(r) + [""] * (cols - len(r))))
    print(line())


def cmd_start(args):
    cfg = Config.load(args.config)
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host
    if args.upstream:
        cfg.default_upstream = args.upstream
    if args.daily is not None:
        cfg.budget_daily = args.daily
    if args.monthly is not None:
        cfg.budget_monthly = args.monthly
    from .server import run
    run(cfg)


def cmd_stats(args):
    cfg = Config.load(args.config)
    store, meter = _ctx(cfg)
    s, e = parse_range(args.range)
    s_data = store.summary(s, e, args.project, args.model)
    if args.json:
        print(json.dumps(s_data, ensure_ascii=False, indent=2))
        return
    if not s_data["requests"]:
        print("暂无调用记录。先跑 `python -m tokenlens start` 把 base_url 指向代理，"
              "或 `python -m tokenlens seed-demo` 灌入演示数据。")
        return
    print(f"\n范围: {args.range}" + (f"  项目: {args.project}" if args.project else "")
          + (f"  模型: {args.model}" if args.model else ""))
    _table(["指标", "数值"], [
        ["请求数", f"{s_data['requests']:,}"],
        ["失败数", f"{s_data['errors']:,}  (错误率 {s_data['error_rate']*100:.2f}%)"],
        ["输入 tokens", f"{s_data['prompt_tokens']:,}"],
        ["输出 tokens", f"{s_data['completion_tokens']:,}"],
        ["缓存命中", f"{s_data['cached_tokens']:,}"],
        ["推理 tokens", f"{s_data['reasoning_tokens']:,}"],
        ["总 tokens", f"{s_data['total_tokens']:,}"],
        ["成本 (USD)", f"${s_data['cost']:.4f}"],
        ["成本 (CNY)", f"¥{s_data['cost']*cfg.usd_cny_rate:.2f}"],
        ["平均延迟", f"{s_data['avg_latency']:.0f} ms"],
        ["P95 延迟", f"{s_data['p95_latency']:.0f} ms"],
        ["平均首字延迟", f"{s_data['avg_ttft']:.0f} ms"],
        ["流式请求", f"{s_data['streamed']:,}"],
    ], ["l", "r"])
    b = meter.budget_status()
    for scope, label in (("daily", "今日"), ("monthly", "本月")):
        d = b[scope]
        if d["limit"]:
            flag = " ⚠ 超支" if d["exceeded"] else (" ⚠ 接近阈值" if d["alert"] else "")
            print(f"{label}预算: ${d['spent']:.4f} / ${d['limit']:.2f}  ({d['ratio']*100:.0f}%){flag}")
    print()


def cmd_top(args):
    cfg = Config.load(args.config)
    store, _ = _ctx(cfg)
    s, e = parse_range(args.range)
    rows = store.breakdown(args.field, s, e, args.project, args.model, limit=args.limit)
    if not rows:
        print("暂无数据")
        return
    data = [[r["name"], f"{r['requests']:,}", f"{r['prompt_tokens']:,}",
             f"{r['completion_tokens']:,}", f"{r['total_tokens']:,}",
             f"${r['cost']:.4f}", f"¥{r['cost']*cfg.usd_cny_rate:.2f}",
             f"{r['avg_latency']:.0f}"] for r in rows]
    _table([args.field, "请求", "输入", "输出", "总计", "成本USD", "成本CNY", "延迟ms"],
           data, ["l", "r", "r", "r", "r", "r", "r", "r"])


def cmd_export(args):
    cfg = Config.load(args.config)
    store, _ = _ctx(cfg)
    s, e = parse_range(args.range)
    rows = store.recent(args.limit if args.limit else 100000, s, e, args.project, args.model)
    if not rows:
        print("暂无数据")
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in reversed(rows):  # recent 是时间倒序，导出改为正序
            r = dict(r)
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            w.writerow(r)
    print(f"已导出 {len(rows)} 条 -> {out}")


def cmd_pricing(args):
    cfg = Config.load(args.config)
    table = PricingTable(cfg.pricing_overrides)
    if args.set_model:
        pin, pout = args.price
        cfg.pricing_overrides[args.set_model] = {"in": pin, "out": pout}
        cfg.save()
        print(f"已设置 {args.set_model}: in=${pin}/1M out=${pout}/1M -> {DEFAULT_CONFIG_PATH}")
        return
    model = args.model
    if model:
        p = table.lookup(model)
        if not p:
            print(f"未知模型 {model}，可用 --set-model 添加")
            return
        print(f"{model}: 输入 ${p[0]}/1M tokens, 输出 ${p[1]}/1M tokens")
        return
    rows = [[k, f"${v['in']}", f"${v['out']}"] for k, v in table.as_dict().items()]
    _table(["模型（模糊匹配前缀）", "输入 /1M", "输出 /1M"], rows, ["l", "r", "r"])
    print(f"\n共 {len(rows)} 条。修改价格: tokenlens pricing --set-model <name> 1.0 4.0")


def cmd_budget(args):
    cfg = Config.load(args.config)
    store, meter = _ctx(cfg)
    if args.daily is not None:
        cfg.budget_daily = args.daily
    if args.monthly is not None:
        cfg.budget_monthly = args.monthly
    cfg.save()
    b = meter.budget_status()
    for scope in ("daily", "monthly"):
        d = b[scope]
        print(f"{scope}: 已用 ${d['spent']:.4f} / 预算 ${d['limit']:.2f} ({d['ratio']*100:.0f}%)")


def cmd_alerts(args):
    cfg = Config.load(args.config)
    store, _ = _ctx(cfg)
    rows = store.alerts_list(limit=args.limit)
    if not rows:
        print("暂无告警记录")
        return
    data = [[time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
             r["scope"], r["message"],
             "超支" if r["spent"] >= r["limit_usd"] else "接近阈值"] for r in rows]
    _table(["时间", "范围", "消息", "级别"], data, ["l", "l", "l", "l"])


def cmd_seed(args):
    from .demo import seed_cli
    seed_cli(args)


def cmd_reset(args):
    cfg = Config.load(args.config)
    store, _ = _ctx(cfg)
    if not args.yes:
        ans = input(f"确认清空 {cfg.db_path} 的所有用量记录？[y/N] ")
        if ans.strip().lower() != "y":
            print("已取消")
            return
    store.clear()
    print("已清空用量记录")


def cmd_config(args):
    cfg = Config.load(args.config)
    if args.init:
        p = cfg.save()
        print(f"配置文件已生成: {p}")
        return
    if args.action == "get":
        key = args.key
        if key not in cfg.__dict__:
            print(f"配置项不存在: {key}")
            sys.exit(1)
        v = cfg.__dict__[key]
        print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        return
    if args.action == "set":
        key, raw = args.key, args.value
        if key not in cfg.__dict__:
            keys = ", ".join(sorted(k for k in cfg.__dict__ if not k.startswith("_")))
            print(f"配置项不存在: {key}（可用: {keys}）")
            sys.exit(1)
        old = cfg.__dict__[key]
        try:
            if isinstance(old, bool):
                val = raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                val = int(raw)
            elif isinstance(old, float):
                val = float(raw)
            elif isinstance(old, dict):
                val = json.loads(raw)
            else:
                val = raw
        except Exception as exc:
            print(f"无法把 {raw!r} 解析为 {type(old).__name__}: {exc}")
            sys.exit(1)
        setattr(cfg, key, val)
        p = cfg.save()
        print(f"{key} = {json.dumps(val, ensure_ascii=False)}  → 已保存 {p}")
        return
    print(json.dumps(cfg.__dict__, ensure_ascii=False, indent=2))


def cmd_live(args):
    """实时查看近 60 秒流量。"""
    cfg = Config.load(args.config)
    store = Store(cfg.db_path)
    d = store.live_stats(time.time() - 60)
    print(f"近 60 秒: {d['requests']} req/min · {int(d['tokens']):,} tokens · 错误 {d['errors']} · 花费 ${d['cost']:.6f}")


def cmd_doctor(args):
    """自检：依赖、数据库、代理连通性。"""
    cfg = Config.load(args.config)
    print("TokenLens 自检")
    print("-" * 46)
    ok = True
    try:
        import fastapi, httpx, uvicorn  # noqa
        print("  [ok]   FastAPI / httpx / uvicorn")
    except Exception as exc:
        ok = False
        print(f"  [fail] 依赖缺失: {exc}")
    try:
        import tiktoken  # noqa
        print("  [ok]   tiktoken（精确计数）")
    except Exception:
        print("  [warn] tiktoken 未安装，将使用启发式估算")
    try:
        store = Store(cfg.db_path)
        n = store.summary()["requests"]
        size = Path(cfg.db_path).stat().st_size / 1024 / 1024
        last = store._local.execute("SELECT MAX(ts) FROM requests").fetchone()[0]
        ago = f"{int(time.time() - last)}s 前" if last else "无"
        print(f"  [ok]   数据库 {cfg.db_path}（{n:,} 条记录 · {size:.1f} MB · 最近写入 {ago}）")
    except Exception as exc:
        ok = False
        print(f"  [fail] 数据库: {exc}")
    print(f"  [info] 代理端口 {cfg.port}，默认上游 {cfg.default_upstream}")
    print(f"  [info] 仪表盘 http://127.0.0.1:{cfg.port}")
    print("-" * 46)
    print("状态:", "正常" if ok else "存在问题")
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="tokenlens",
        description="TokenLens · AI Token 用量监控（本地代理 + 仪表盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  tokenlens start --port 8787                 启动代理与仪表盘
  tokenlens start --upstream https://api.deepseek.com/v1
  tokenlens stats --range 7d                  查看 7 天汇总
  tokenlens top --field project --range 30d   按项目排行
  tokenlens export --out usage.csv            导出 CSV
  tokenlens seed-demo                         灌入演示数据
  tokenlens live                              实时查看近 60 秒流量
  tokenlens config set budget_daily 5         修改日预算
  tokenlens config get dashboard_token        查看访问令牌
""")
    p.add_argument("--config", help="配置文件路径（默认 ~/.tokenlens/config.json）")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("start", help="启动代理与仪表盘")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--upstream", default=None, help="默认上游 base url")
    s.add_argument("--daily", type=float, default=None, help="日预算 USD")
    s.add_argument("--monthly", type=float, default=None, help="月预算 USD")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stats", help="用量汇总")
    s.add_argument("--range", default="today", help="today|24h|7d|30d|month|all")
    s.add_argument("--project", default=None)
    s.add_argument("--model", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("top", help="排行")
    s.add_argument("--field", default="model", choices=["model", "project", "provider", "endpoint", "day"])
    s.add_argument("--range", default="today")
    s.add_argument("--project", default=None)
    s.add_argument("--model", default=None)
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("export", help="导出 CSV")
    s.add_argument("--out", default="tokenlens-usage.csv")
    s.add_argument("--range", default="30d")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--project", default=None)
    s.add_argument("--model", default=None)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("pricing", help="查看或设置模型价格")
    s.add_argument("--model", default=None, help="查询某个模型的匹配价格")
    s.add_argument("--set-model", dest="set_model", default=None)
    s.add_argument("price", nargs="*", type=float, help="输入价 输出价（USD/1M）")
    s.set_defaults(func=cmd_pricing)

    s = sub.add_parser("budget", help="设置预算")
    s.add_argument("--daily", type=float, default=None)
    s.add_argument("--monthly", type=float, default=None)
    s.set_defaults(func=cmd_budget)

    s = sub.add_parser("alerts", help="查看预算告警历史")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_alerts)

    s = sub.add_parser("seed-demo", help="灌入演示数据")
    s.add_argument("--n", type=int, default=600)
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("reset", help="清空用量数据")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("config", help="查看/修改配置（config get/set KEY [VALUE]）")
    s.add_argument("--init", action="store_true", help="生成默认配置文件")
    s.add_argument("action", nargs="?", choices=["get", "set"], metavar="ACTION", help="get 查看 / set 修改")
    s.add_argument("key", nargs="?", metavar="KEY", help="配置项名")
    s.add_argument("value", nargs="?", metavar="VALUE", help="新值（set 时必填）")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("live", help="实时查看近 60 秒流量")
    s.set_defaults(func=cmd_live)

    s = sub.add_parser("doctor", help="环境自检")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
