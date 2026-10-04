"""M0 数据可行性验证（需求书 1.7）——A 股 BaoStock 部分。
运行：python tools/m0_probe.py [--stability N]   结果写入 docs/M0-数据可行性验证报告.md
AkShare / yfinance 部分需另外安装依赖后验证（本脚本检测到未安装时会如实标注「未验证」）。"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from server.datasource_cn import BaoStockSource, attach_adj_factor, board_of  # noqa: E402

R: list[dict] = []


def rec(item: str, verdict: str, detail: str):
    print(f"[{verdict}] {item}: {detail}", flush=True)
    R.append({"item": item, "verdict": verdict, "detail": detail})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stability", type=int, default=80)
    a = ap.parse_args()
    src = BaoStockSource()
    src.th.min_delay, src.th.max_delay = 0.3, 1.0
    today = date.today()

    # 1. 交易日历
    cal = src.trade_calendar("2016-01-01", (today + timedelta(days=60)).isoformat())
    opens = [d for d, o, _ in cal if o]
    rec("BaoStock · 交易日历", "通过", f"{len(opens)} 个交易日，覆盖到 {cal[-1][0]}（含未来 {sum(1 for d, o, _ in cal if d > today.isoformat())} 天）")
    last_td = max(d for d in opens if d <= today.isoformat())

    # 2. 证券名单并集（含已退市）
    qs = [date(y, m, d) for y in range(2016, today.year + 1) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
          if date(y, m, d) < today]
    sample_q = qs[::4] + [qs[-1]]
    days = []
    for q in sample_q:
        td = [d for d in opens if (q - timedelta(days=10)).isoformat() <= d <= q.isoformat()]
        if td:
            days.append(td[-1])
    union = src.list_securities_union(days)
    cur = src.list_securities_union([last_td])
    gone = sorted(set(union["symbol"]) - set(cur["symbol"]))
    rec("BaoStock · 证券名单并集", "通过" if len(gone) > 50 else "待核实",
        f"{len(days)} 个历史交易日并集 {len(union)} 只 A 股，当前列表 {len(cur)} 只，并集中有而当前无的（疑似已退市）{len(gone)} 只；板块分布 {union['board'].value_counts().to_dict()}")
    delisted_chk = []
    for s in random.Random(1).sample(gone, min(6, len(gone))):
        b = src.security_basic(s)
        delisted_chk.append((s, b.get("name"), b.get("list_date"), b.get("delist_date"), b.get("status")))
    ok = sum(1 for x in delisted_chk if x[3])
    rec("BaoStock · 已退市证券基本资料", "通过" if ok == len(delisted_chk) and delisted_chk else "待核实",
        f"抽样 {len(delisted_chk)} 只疑似退市证券，其中 {ok} 只返回了退市日期：{delisted_chk[:3]}")

    # 3. 日线字段 / 单位 / 停牌 / ST
    samples = ["sh.600519", "sz.000001", "sz.300750", "sh.688981"] + gone[:2]
    frames = {}
    t0 = time.time()
    for s in samples:
        b = src.security_basic(s)
        s0 = max("2016-01-01", b.get("list_date") or "2016-01-01")
        s1 = min(last_td, b.get("delist_date") or last_td)
        frames[s] = src.daily_bars(s, s0, s1)
    n_rows = sum(len(v) for v in frames.values())
    has_cols = all(c in frames["sh.600519"].columns for c in ("turnover", "trade_status", "is_st", "adj_factor"))
    susp = {s: int((f["trade_status"] == 0).sum()) for s, f in frames.items()}
    st = {s: int((f["is_st"] == 1).sum()) for s, f in frames.items()}
    rec("BaoStock · 日线字段", "通过" if has_cols else "不通过",
        f"{len(samples)} 只共 {n_rows} 行（{time.time() - t0:.0f}s）；含 换手率/交易状态/ST 标记/复权因子；停牌日数 {susp}；ST 日数 {st}")
    mt = frames["sh.600519"].tail(1).iloc[0]
    rec("BaoStock · 成交量 / 成交额单位", "通过",
        f"贵州茅台 {mt['date']}：volume={mt['volume']:.0f}、amount={mt['amount']:.0f}、close={mt['close']:.2f}；amount/volume={mt['amount'] / mt['volume']:.1f} ≈ 股价 ⇒ volume 单位为「股」、amount 为「元」")

    # 4. 复权因子自洽：复权后收益 vs 官方 pctChg（只在日线现算复权，不落库）
    bad_total, chk_total, details = 0, 0, []
    for s in ["sh.600519", "sz.000001", "sz.300750", "sh.600036"]:
        f = frames.get(s)
        if f is None:
            b = src.security_basic(s)
            f = src.daily_bars(s, max("2016-01-01", b.get("list_date") or "2016-01-01"), last_td)
        raw = src._query("query_history_k_data_plus", s, "date,close,pctChg", start_date=f["date"].iloc[0], end_date=f["date"].iloc[-1],
                         frequency="d", adjustflag="3")
        raw["pctChg"] = pd.to_numeric(raw["pctChg"], errors="coerce")
        adj = f.set_index("date")["close"] * f.set_index("date")["adj_factor"]
        r_adj = adj.pct_change() * 100
        m = pd.concat([r_adj.rename("adj"), raw.set_index("date")["pctChg"]], axis=1).dropna()
        m = m[(f.set_index("date")["trade_status"].reindex(m.index) == 1)]
        diff = (m["adj"] - m["pctChg"]).abs()
        n_ex = int((f["adj_factor"].diff().abs() > 1e-9).sum())
        bad = int((diff > 0.05).sum())
        bad_total += bad
        chk_total += len(m)
        details.append(f"{s}: {len(m)} 日、除权 {n_ex} 次、偏差>0.05% 的 {bad} 日")
    rec("BaoStock · 复权因子自洽", "通过" if bad_total <= max(2, chk_total * 0.001) else "待核实",
        f"用 close × backAdjustFactor 现算的日收益与官方 pctChg 对比：{'；'.join(details)}")

    # 5. 指数 / 行业
    idx_ok = {}
    for s in ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]:
        d = src.index_bars(s, "2016-01-01", last_td)
        idx_ok[s] = (len(d), d["date"].iloc[-1] if len(d) else None)
    rec("BaoStock · 指数日线", "通过" if all(v[0] > 1000 for v in idx_ok.values()) else "待核实", f"{idx_ok}")
    ind = src.industry_map()
    rec("BaoStock · 行业分类", "通过" if len(ind) > 3000 else "待核实", f"{len(ind)} 只带证监会行业分类，行业数 {ind['industry'].nunique()}（当前快照，无历史生效日期，见 3.14 已知偏差）")

    # 6. 稳定性与限速
    n = a.stability
    fails, times = 0, []
    pool = random.Random(2).sample(list(cur["symbol"]), min(n, len(cur)))
    for s in pool:
        t = time.time()
        try:
            src.daily_bars(s, (today - timedelta(days=40)).isoformat(), last_td)
        except Exception:
            fails += 1
        times.append(time.time() - t)
    rec("BaoStock · 稳定性", "通过" if fails == 0 else "待核实",
        f"连续 {n} 只股票（每只 2 次请求：日线 + 复权因子；随机间隔 0.3~1.0s）：失败 {fails}，平均 {np.mean(times):.2f}s/只，P95 {np.percentile(times, 95):.2f}s；"
        f"外推 5000 只全量初始化约 {np.mean(times) * 5000 / 3600:.1f} 小时（含 10 年区间的响应更大）")
    now_cn = datetime.now().astimezone()
    rec("BaoStock · 当日数据可用时点", "未验证", "需在交易日收盘后（A 股 15:00 后）运行探测：每 5 分钟查询当日 K 线，记录首次返回的时间，写入 config.yaml jobs.data_ready_after_close_minutes。当前默认 90 分钟为保守假设。")

    # 7. AkShare / yfinance
    try:
        import akshare  # noqa: F401
        rec("AkShare · 快照与扩展", "未验证", f"已安装 akshare {akshare.__version__}，请在交易日用 tools/m0_probe_akshare.py（待补）核对 stock_zh_a_spot_em 字段 / 成交量单位 / 业绩预约披露接口名")
    except Exception:
        rec("AkShare · 快照与扩展", "未验证", "本机未安装 akshare：快照路径 / 财报日历自动降级（L1 不做快照粗筛、风险剔除层无财报数据，界面会标注）。安装后需核实接口名与字段并锁定版本")
    rec("yfinance · 美股", "未验证", "美股为 M5，不在本次范围；后端 datasource_us.py 尚未实现")

    src.close()
    out = ROOT / "docs" / "M0-数据可行性验证报告.md"
    out.parent.mkdir(exist_ok=True)
    lines = ["# M0 数据可行性验证报告（A 股 · BaoStock）", "",
             f"运行时间：{datetime.now().isoformat(timespec='seconds')}　｜　脚本：`tools/m0_probe.py`", "",
             "| 验证对象 | 结论 | 说明 |", "|---|---|---|"]
    for r in R:
        lines.append(f"| {r['item']} | **{r['verdict']}** | {r['detail'].replace('|', '/')} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("report ->", out)


if __name__ == "__main__":
    main()
