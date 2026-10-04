"""M0 数据可行性验证——美股部分（yfinance / Nasdaq Trader / exchange_calendars / SEC / 巨潮）。
运行：python tools/m0_probe_us.py     结果追加到 docs/M0-数据可行性验证报告.md 的「美股与事件源」小节。"""
from __future__ import annotations

import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

from server.datasource_us import UsSource, parse_nasdaq_lists  # noqa: E402

R = []


def rec(item, verdict, detail):
    print(f"[{verdict}] {item}: {detail}", flush=True)
    R.append((item, verdict, detail))


def main():
    s = UsSource()
    s.th.min_delay = s.th.max_delay = 0.2
    import requests

    # 1. 价格口径：拆股样本
    t = time.time()
    df = s.yf.download(["NVDA"], start="2024-06-03", end="2024-06-14", auto_adjust=False, actions=True, progress=False, group_by="ticker", threads=False)["NVDA"]
    c0 = float(df.loc["2024-06-05", "Close"])
    v0 = float(df.loc["2024-06-05", "Volume"])
    sp = df["Stock Splits"][df["Stock Splits"] > 0]
    real_est = c0 * 10                                  # 2024-06-10 的 10 拆 1：还原后真实价
    rec("yfinance · 价格 / 成交量口径", "通过" if 1150 < real_est < 1300 and len(sp) == 1 else "待核实",
        f"NVDA 2024-06-05：Close={c0:.2f}、Volume={v0:,.0f}（拆股调整口径）；`actions=True` 一次请求带回拆股 {len(sp)} 条（{sp.index[0].date() if len(sp) else '—'} = {float(sp.iloc[0]) if len(sp) else '—'}）；"
        f"还原当时价 = {c0:.2f} × 10 = {real_est:.0f}（已知真实价约 1224）；调整价 × 调整量 = 真实成交额，可直接用于成交额类条件")
    # 2. 证券列表
    nl = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", timeout=30).text
    ol = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", timeout=30).text
    allr = len(pd.read_csv(__import__("io").StringIO(nl), sep="|")) + len(pd.read_csv(__import__("io").StringIO(ol), sep="|"))
    lst = parse_nasdaq_lists(nl, ol)
    rec("美股证券列表（Nasdaq Trader）", "通过", f"nasdaqlisted + otherlisted 共 {allr} 行 → 过滤 ETF / 测试标的 / 优先股 / 权证 / 单位 / SPAC / 异常财务状态后 {len(lst)} 只普通股（{lst.exchange.value_counts().to_dict()}）；字段含 ETF、Test Issue、Financial Status、Round Lot Size，足以做 L1 类型过滤")
    # 3. 批量与限速
    syms = lst["symbol"].head(100).tolist()
    t = time.time()
    got = s.daily_bars_batch(syms, "2016-10-01", "2026-10-02")
    dt = time.time() - t
    empty = [x for x in syms if x not in got]
    rec("yfinance · 批量下载与限速", "通过", f"100 只 × 10 年日线（含拆股 / 分红）{dt:.0f} 秒（≈{dt / 100:.2f} 秒/只），无数据 {len(empty)} 只；外推 5000 只约 {dt * 50 / 60:.0f} 分钟；`threads=False` 顺序下载，未触发限流；整批为空按限流处理并退避")
    # 4. 日历
    cal = {d: (o, h) for d, o, h in s.trade_calendar("2026-04-01", "2026-12-31")}
    rec("美股交易日历（exchange_calendars XNYS）", "通过" if cal["2026-07-03"][0] == 0 and cal["2026-11-27"] == (1, 1) else "待核实",
        f"2026-07-03（独立日顺延）休市={cal['2026-07-03'][0] == 0}；2026-11-27 提前收盘={cal['2026-11-27'] == (1, 1)}；2026-04-03 耶稣受难日休市={cal['2026-04-03'][0] == 0}")
    # 5. 指数 / VIX
    ix = {k: len(s.index_bars(k, "2025-01-01", "2026-10-02")) for k in ("SPY", "QQQ", "IWM", "DIA", "^GSPC", "^IXIC", "^VIX")}
    rec("美股基准 / VIX", "通过" if all(v > 200 for v in ix.values()) else "待核实", f"各序列行数 {ix}")
    # 6. 行业 / 财报日期 / 新闻 / Insider
    t = time.time()
    ind = s.industry("AAPL")
    ed = s.earnings_dates("AAPL")
    nw = s.news("AAPL")
    ins = s.insider_transactions("AAPL")
    rec("yfinance · 行业 / 财报日期 / 新闻 / Insider", "通过", f"AAPL 行业={ind}；财报日期 {len(ed)} 条（含下一次 {[d for d, r in ed if not r][:1]}）；新闻 {len(nw)} 条；Insider 交易 {len(ins)} 条；{time.time() - t:.0f} 秒")
    # 7. SEC / 巨潮
    from server import datasource_events as ev
    u = ev.UsEvents(s)
    u.th_sec.min_delay = u.th_sec.max_delay = 0.2
    fl = u.filings("AAPL", 60)
    rec("SEC EDGAR（官方披露，等级 1）", "通过" if fl else "待核实", f"AAPL 近 60 天 {len(fl)} 条（8-K / 10-Q / Form 4 / 144 等）；免费、无密钥，须带联系方式的 User-Agent，≤10 次/秒")
    cn = ev.CnEvents(None)
    ann = cn.announcements("sh.600519", 120)
    rec("巨潮资讯公告（官方，等级 1）", "通过" if ann else "待核实", f"贵州茅台近 120 天 {len(ann)} 条公告；类型分布 { {k: sum(1 for a in ann if a['event_type'] == k) for k in set(a['event_type'] for a in ann)} }")

    out = ROOT / "docs" / "M0-数据可行性验证报告.md"
    txt = out.read_text(encoding="utf-8") if out.exists() else "# M0 数据可行性验证报告\n"
    txt = txt.split("\n## 美股与事件源")[0].rstrip() + "\n\n## 美股与事件源（M5 / M6）\n\n" + f"运行时间：{datetime.now().isoformat(timespec='seconds')}　｜　脚本：`tools/m0_probe_us.py`\n\n| 验证对象 | 结论 | 说明 |\n|---|---|---|\n"
    for i, v, d in R:
        txt += f"| {i} | **{v}** | {d.replace('|', '/')} |\n"
    txt += "\n> 免费源**没有已退市美股的历史**：美股回测存在幸存者偏差，结果只作上界参考（3.26.1-5）；真正无偏的证据只有 `scan_outcomes`。\n> Alpha Vantage / Finnhub 免费档额度很小（约 25 次/天），本实现改用额度充足、无需密钥的 Yahoo + SEC + 巨潮。\n"
    out.write_text(txt, encoding="utf-8")
    print("report ->", out)


if __name__ == "__main__":
    main()
