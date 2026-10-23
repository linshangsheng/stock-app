"""A 股恐慌指数（QVIX）研究：分区线 + 处在各区之后指数 20 / 60 个交易日的涨跌。写入 server/qvix_evidence.json。

分区（事先定好，不调参）：美股 VIX 的分区线 15 / 20 / 25 / 30 / 40 在 VIX 1990 年以来的长历史里各对应一个分位（约 35% / 65% / 82% / 91% / 97%），
每条 QVIX 取它自己历史上同样的分位作为分区线——「恐慌」在两边都表示「处在自己历史的前约 9%」，不把美股的数值照搬到 A 股。
前瞻收益：QVIX300 对沪深300、QVIXCYB 对创业板指（价格指数），次日收盘起算；样本重叠，同时给出进入该区的独立次数。
样本只有 2019-12（沪深300ETF 期权）/ 2022-09（创业板ETF 期权）以来，偏短：结论只作参考。

用法：python tools/qvix_research.py（先运行一次 python -m server.qvix 把 QVIX 历史存进 A 股库）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.vix_research import fetch, stats_for, CACHE, HZ  # noqa: E402

OUT = ROOT / "server" / "qvix_evidence.json"
VIX_CUTS = [15, 20, 25, 30, 40]
PAIRS = {"QVIX300": "sh.000300", "QVIXCYB": "sz.399006"}


def main(db_path: str | None = None):
    fetch("^VIX", "VIX")
    vix = pd.read_csv(CACHE / "VIX.csv", index_col=0)["Close"].astype(float).dropna()
    pcts = [float((vix < c).mean()) for c in VIX_CUTS]
    con = sqlite3.connect(f"file:{db_path or ROOT / 'data' / 'ashare.db'}?mode=ro", uri=True)
    rd = lambda s: pd.read_sql("SELECT date, close FROM index_bar WHERE symbol=? ORDER BY date", con, params=(s,)).set_index("date")["close"].astype(float)
    res = {"generated": date.today().isoformat(), "vix_cut_percentiles": [round(p, 3) for p in pcts], "series": {},
           "rule": "分区线 = 美股 VIX 15/20/25/30/40 在 VIX 长历史中的分位，套用到每条 QVIX 自己的历史；前瞻收益为对应指数价格，次日起算"}
    print("VIX 分区线对应的历史分位：", ", ".join(f"{c} → {p:.0%}" for c, p in zip(VIX_CUTS, pcts)))
    for sym, idx in PAIRS.items():
        q, px = rd(sym), rd(idx)
        if len(q) < 250:
            print(sym, "数据不足，跳过")
            continue
        cuts = [round(float(q.quantile(p)) * 2) / 2 for p in pcts]
        df = pd.concat({"q": q, "px": px}, axis=1).dropna()
        split = df.index[int(len(df) * 0.6)]
        bounds = [0, *cuts, 999]
        zones = [{"lo": bounds[i], "hi": bounds[i + 1], **stats_for((df["q"] >= bounds[i]) & (df["q"] < bounds[i + 1]), df["px"], split)} for i in range(6)]
        un = {}
        for h in HZ:
            f = (df["px"].shift(-h - 1) / df["px"].shift(-1) - 1).dropna()
            un[f"fwd{h}"] = {"mean": round(float(f.mean()), 4), "win": round(float((f > 0).mean()), 3)}
        res["series"][sym] = {"index": idx, "from": df.index[0], "to": df.index[-1], "split": split, "cuts": cuts, "zones": zones, "unconditional": un,
                              "quantiles": {str(p): round(float(q.quantile(p / 100)), 1) for p in (10, 25, 50, 75, 90, 95)}}
        print(f"\n{sym}（{df.index[0]} ~ {df.index[-1]}，{len(df)} 天）分区线 {cuts}；所有日子 60 日 {un['fwd60']['mean']:+.1%} / {un['fwd60']['win']:.0%}")
        for z in zones:
            a, b = z["fwd20"], z["fwd60"]
            fm = lambda x: "—" if x is None else f"{x:+.1%}"
            print(f"  [{z['lo']:>5},{z['hi']:>5})  20日 {fm(a['mean'])} {a['win'] if a['win'] is None else format(a['win'], '.0%')}   "
                  f"60日 {fm(b['mean'])} {b['win'] if b['win'] is None else format(b['win'], '.0%')} 最差 {fm(b['worst'])}   ({z['days']} 天 / {z['episodes']} 次)")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写入", OUT)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
