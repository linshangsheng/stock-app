"""命令行入口：python -m server.cli <command>
  init [--demo] [--limit N]   首次初始化（日历 / 名单 / L1 / 10 年日线 / 指数 / 行业），可中断续跑
  update                       盘后任务链：数据更新 -> 闸门 -> 扫描 -> 体检 -> 回填 -> 备份
  scan [--date YYYY-MM-DD]     只跑选股扫描
  health                       只跑持仓体检
  backtest --kind K [--start --end --n-random]   回测 / 因子检验 / 事件研究 / 消融
  backup / restore --date D --what portfolio|ashare
  l1-bias [--sample N]         L1 快照偏差对比实验（3.26.1-6）：抽样补拉被 L1 剔除的股票，比较含 / 不含的回测差异
  earnings                     预取 L1 的财报日历（可选，约 1~2 小时；日常按需拉取）
  serve [--demo]               启动本地服务（等同 python -m server.main）
--demo：使用合成演示数据（写入 data/demo/，**不代表真实行情**）。"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _prepare_demo(demo: bool):
    if not demo:
        return
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    d = root / "data" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    os.environ["STOCK_DATA_DIR"] = str(d)
    up = d / "user_config.yaml"
    if not up.exists() or "us: demo" not in up.read_text(encoding="utf-8"):
        up.write_text("datasource:\n  cn: demo\n  us: demo\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="server.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "update", "scan", "health", "backtest", "backup", "restore", "serve", "l1-bias", "earnings"):
        p = sub.add_parser(name)
        p.add_argument("--demo", action="store_true")
        p.add_argument("--market", default="CN")
        p.add_argument("--limit", type=int)
        p.add_argument("--date")
        p.add_argument("--kind", default="strategy")
        p.add_argument("--start")
        p.add_argument("--end")
        p.add_argument("--n-random", type=int, default=200)
        p.add_argument("--what", default="portfolio")
        p.add_argument("--sample", type=int, default=100)
    a = ap.parse_args(argv)
    _prepare_demo(a.demo)

    from . import backup, backtest, db, jobs, portfolio, scanner, settings  # noqa: E402 - 须在设置环境变量之后导入

    if a.cmd == "serve":
        from . import main as m
        return m.main()
    jm = jobs.JobManager()
    jm.progress_print = True

    def prog(name, i, n):
        if i % max(1, n // 20) == 0 or i == n:
            print(f"  [{name}] {i}/{n}", flush=True)
    jm._progress = prog
    if a.cmd == "init":
        out = jm.init(a.market, limit=a.limit)
    elif a.cmd == "update":
        out = jm.daily_chain(a.market, scan_day=a.date)
    elif a.cmd == "scan":
        r = scanner.run_scan(a.market, scan_date=a.date)
        out = {k: v for k, v in r.items() if k != "candidates"} | {"candidates": [
            {k: c[k] for k in ("symbol", "name", "setup_label", "score", "trigger_price", "stop_price", "shares", "fit", "skip_reason")}
            for c in r.get("candidates", [])]}
    elif a.cmd == "health":
        out = portfolio.health(a.market, a.date)
    elif a.cmd == "backtest":
        params = {"strategy": {k: v for k, v in (("start", a.start), ("end", a.end)) if v}, "n_random": a.n_random}
        out = backtest.run_job(a.market, a.kind, params)
        out.pop("trades", None)
        out.pop("equity_curve", None)
        out.pop("drawdown_curve", None)
        out.pop("benchmark_curve", None)
    elif a.cmd == "l1-bias":
        from . import l1_bias
        out = l1_bias.run(a.market, sample=a.sample, start=a.start, progress=prog)
    elif a.cmd == "earnings":
        from . import ingest
        from .datasource_cn import get_source
        src = get_source(a.market)
        with settings.market_ctx(a.market), db.market_db(a.market) as c:
            out = ingest.refresh_earnings_bulk(c, src, a.market, progress=prog)
    elif a.cmd == "backup":
        out = backup.run_backup()
    elif a.cmd == "restore":
        out = backup.restore(a.date, a.what)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
