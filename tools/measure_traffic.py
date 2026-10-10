"""实测各类初始化请求的下载流量（用 Windows 网卡接收字节计数前后差；测量时请尽量关闭其他联网程序，结果含少量背景噪声）。
用法：python tools/measure_traffic.py"""
from __future__ import annotations

import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def rx() -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-NetAdapterStatistics | Measure-Object -Property ReceivedBytes -Sum).Sum"], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def measure(label, fn, units, unit_name):
    time.sleep(2)
    a = rx()
    t = time.time()
    fn()
    time.sleep(2)
    b = rx()
    mb = (b - a) / 1e6
    print(f"{label}: {mb:.2f} MB / {units} {unit_name} = {mb * 1000 / units:.1f} KB/{unit_name}  ({time.time() - t:.0f}s)", flush=True)
    return mb / units


def main():
    from server.datasource_cn import BaoStockSource
    from server.datasource_us import UsSource
    import io
    import requests
    import pandas as pd

    # 背景噪声：空等 10 秒
    a = rx(); time.sleep(10); noise = (rx() - a) / 1e6 / 10
    print(f"背景流量约 {noise * 1000:.0f} KB/s（下面的结果已含这部分噪声）")

    bs = BaoStockSource()
    bs.th.min_delay = bs.th.max_delay = 0.0
    syms = ["sh.600519", "sz.000001", "sz.300750", "sh.600036", "sh.601318", "sz.000858", "sh.600900", "sz.002594", "sh.601888", "sh.600276"]
    bs._query("query_trade_dates", start_date="2026-09-01", end_date="2026-09-30")          # 先登录，排除登录流量
    per = measure("BaoStock 单只 10 年日线 + 复权因子 + 基本资料", lambda: [(bs.security_basic(s), bs.daily_bars(s, "2016-10-01", "2026-09-30")) for s in syms], len(syms), "只")
    measure("BaoStock 单只近 45 天（预筛）", lambda: [bs.daily_bars(s, "2026-08-15", "2026-09-30", prev_factor=1.0) for s in syms], len(syms), "只")
    measure("BaoStock query_all_stock（一个历史交易日）", lambda: bs._query("query_all_stock", day="2026-09-30"), 1, "次")
    measure("BaoStock 行业分类（全市场一次）", lambda: bs.industry_map(), 1, "次")
    measure("BaoStock 6 条指数 10 年", lambda: [bs.index_bars(s, "2016-10-01", "2026-09-30") for s in ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]], 6, "条")

    us = UsSource()
    us.th.min_delay = us.th.max_delay = 0.0
    nl = None

    def lists():
        nonlocal nl
        nl = us.list_current_securities()
    measure("Nasdaq Trader 两个清单", lists, 1, "次")
    tick = nl["symbol"].head(100).tolist()
    measure("yfinance 批量 100 只 10 年日线（含拆股 / 分红）", lambda: us.daily_bars_batch(tick, "2016-10-01", "2026-10-02"), 100, "只")
    measure("yfinance 批量 100 只 近 45 天（预筛）", lambda: us.daily_bars_batch(tick, "2026-08-15", "2026-10-02"), 100, "只")
    measure("yfinance 单只行业 info", lambda: us.industry("AAPL"), 1, "只")
    measure("yfinance 7 条基准 + 11 只行业 ETF 各 10 年", lambda: [us.index_bars(s, "2016-10-01", "2026-10-02") for s in ["SPY", "QQQ", "IWM", "DIA", "^GSPC", "^IXIC", "^VIX", "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLU", "XLB", "XLRE", "XLC"]], 18, "条")


if __name__ == "__main__":
    main()
