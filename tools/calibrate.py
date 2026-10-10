"""个股策略参数校准（抽样回测）：样本内（IS）挑选、样本外（OOS）检验，避免在同一段历史上「调到最好看」。
内存不够跑全市场时，按固定随机种子抽取一部分股票（含已退市，避免幸存者偏差）。
用法：python tools/calibrate.py [--n 1200] [--seed 7] [--start 2018-01-01] [--split 2023-01-01] [--out data/calibration.json]
结果只供参考：变体越多，越容易碰巧挑到好看的组合（每个变体都计入试验次数，见 Deflated Sharpe）。"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VARIANTS = [
    ("基准（当前默认）", {}),
    ("市场环境闸门：关", {"funnel": {"regime_gate": False}}),
    ("止损 ATR×1.5", {"exits": {"stop_atr_k": 1.5}}),
    ("止损 ATR×3", {"exits": {"stop_atr_k": 3.0}}),
    ("止损：结构化（形态低点）", {"exits": {"stop_mode": "structure"}}),
    ("移动止盈 ATR×2", {"exits": {"trail_atr_k": 2.0}}),
    ("移动止盈 ATR×4", {"exits": {"trail_atr_k": 4.0}}),
    ("止盈：跟踪 MA10", {"exits": {"trail": "ma10"}}),
    ("止盈：分批（1.5R 卖一半）", {"exits": {"partial_r": 1.5, "partial_fraction": 0.5}}),
    ("止盈：固定 2R（对照）", {"exits": {"trail": "none", "take_profit_r": 2.0}}),
    ("最长持有 10 天", {"exits": {"max_hold_days": 10}}),
    ("最长持有 30 天", {"exits": {"max_hold_days": 30}}),
    ("入场：次日开盘", {"entry_mode": "next_open"}),
    ("入场：触发价", {"entry_mode": "stop_entry"}),
    ("候选：全量（不取 Top N）", {"funnel": {"top_n": 0}}),
    ("仓位：等权", {"portfolio": {"sizing": "equal"}}),
    ("只做突破", {"setups": ["breakout"]}),
    ("只做回踩", {"setups": ["pullback"]}),
    ("只做波动收缩突破", {"setups": ["vcp"]}),
]
# 第二组：强势超跌（短期反转）作为独立策略，以及与波动收缩突破的组合。只测这几组事先写好的出场设定。
OVERSOLD_VARIANTS = [
    # 反转持仓本来就在 MA20 下方：必须关掉「收盘跌破 MA20 就卖」（exit_below_ma: 0），否则次日就被卖出
    ("超跌：默认出场（含跌破MA20卖，对照）", {"setups": ["oversold"]}),
    ("超跌：不看MA20、持有20天", {"setups": ["oversold"], "exits": {"exit_below_ma": 0}}),
    ("超跌：不看MA20、持有10天", {"setups": ["oversold"], "exits": {"exit_below_ma": 0, "max_hold_days": 10}}),
    ("超跌：不看MA20、10天、不移动止损", {"setups": ["oversold"], "exits": {"exit_below_ma": 0, "max_hold_days": 10, "trail": "none"}}),
    ("超跌：不看MA20、10天、止损ATR×3", {"setups": ["oversold"], "exits": {"exit_below_ma": 0, "max_hold_days": 10, "stop_atr_k": 3.0}}),
    ("超跌：不看MA20、10天、闸门关", {"setups": ["oversold"], "exits": {"exit_below_ma": 0, "max_hold_days": 10}, "funnel": {"regime_gate": False}}),
    ("超跌+波动收缩：不看MA20、10天", {"setups": ["oversold", "vcp"], "exits": {"exit_below_ma": 0, "max_hold_days": 10}}),
    ("对照：仅波动收缩（默认出场）", {"setups": ["vcp"]}),
    ("对照：当前默认（突破+回踩+波动收缩）", {"setups": ["breakout", "pullback", "vcp"]}),
]
# 第三组：次日开盘买时，高开多少就放弃（事先定好的 4 档）
GAP_VARIANTS = [
    ("高开：不限（当前）", {"max_gap_atr": 0}),
    ("高开 > 0.5×ATR 放弃", {"max_gap_atr": 0.5}),
    ("高开 > 1×ATR 放弃", {"max_gap_atr": 1.0}),
    ("高开 > 1.5×ATR 放弃", {"max_gap_atr": 1.5}),
    ("按开盘价重算股数（风险不变）", {"resize_on_fill": True}),
    ("重算股数 + 高开 > 1×ATR 放弃", {"resize_on_fill": True, "max_gap_atr": 1.0}),
]
# 第四组：短期止盈（事先定好的几种，与「只用移动止损」对比）
TP_VARIANTS = [
    ("止盈：只用移动止损（当前）", {}),
    ("止盈：1R 卖一半 + 移动止损", {"exits": {"partial_r": 1.0, "partial_fraction": 0.5}}),
    ("止盈：1.5R 卖一半 + 移动止损", {"exits": {"partial_r": 1.5, "partial_fraction": 0.5}}),
    ("止盈：2R 卖一半 + 移动止损", {"exits": {"partial_r": 2.0, "partial_fraction": 0.5}}),
    ("止盈：2R 全部卖出", {"exits": {"take_profit_r": 2.0}}),
    ("止盈：3R 全部卖出", {"exits": {"take_profit_r": 3.0}}),
    ("止盈：+10% 全部卖出", {"exits": {"take_profit_pct": 0.10}}),
]
# 第五组：候选排序方式
SCORE_VARIANTS = [
    ("排序：低换手 + 低波动（新）", {"funnel": {"score_method": "lowrisk"}}),
    ("排序：RPS + 行业强弱（旧）", {"funnel": {"score_method": "momentum"}}),
    ("排序：不排序（按信号先后）", {"funnel": {"rps_score": False, "industry_score": False}}),
]
GROUPS = {"score": SCORE_VARIANTS, "default": VARIANTS, "oversold": OVERSOLD_VARIANTS, "gap": GAP_VARIANTS, "tp": TP_VARIANTS}
KEYS = ("n", "expectancy_r", "win_rate", "payoff", "total_return", "cagr", "max_drawdown", "sharpe", "avg_hold_days")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--split", default="2023-01-01")
    ap.add_argument("--out", default="data/calibration.json")
    ap.add_argument("--only", default="", help="只跑名字包含该字符串的变体（调试用）")
    ap.add_argument("--group", default="default", choices=list(GROUPS), help="default = 参数 / 出场变体；oversold = 强势超跌策略")
    a = ap.parse_args()
    from server import backtest, db, settings

    t0 = time.time()
    with settings.market_ctx("CN"), db.market_db("CN") as c:
        pool = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE (in_l1=1 OR status='delisted') AND sec_type='stock' ORDER BY symbol")]
        syms = sorted(random.Random(a.seed).sample(pool, min(a.n, len(pool))))
        end = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        ctx = backtest.BtContext(c, "CN", start=a.start, end=end, symbols=syms)
    print(f"样本 {len(syms)} 只（池 {len(pool)}），{ctx.T} 个交易日，准备耗时 {time.time() - t0:.0f}s", flush=True)
    rows = []
    for label, ov in GROUPS[a.group]:
        if a.only and a.only not in label:
            continue
        row = {"variant": label, "override": ov}
        for part, (s, e) in (("is", (a.start, _prev(a.split))), ("oos", (a.split, end))):
            t1 = time.time()
            with settings.market_ctx("CN"):
                m = ctx.run(settings.deep_merge(ov, {"start": s, "end": e}), detail=False)["metrics"]
            row[part] = {k: m.get(k) for k in KEYS}
            row[part]["secs"] = round(time.time() - t1, 1)
        rows.append(row)
        i, o = row["is"], row["oos"]
        print(f"{label:22s} IS n={i['n']:>4} E[R]={_f(i['expectancy_r'])} 回撤={_f(i['max_drawdown'])} Sharpe={_f(i['sharpe'])} 收益={_f(i['total_return'])} | "
              f"OOS n={o['n']:>4} E[R]={_f(o['expectancy_r'])} 回撤={_f(o['max_drawdown'])} Sharpe={_f(o['sharpe'])} 收益={_f(o['total_return'])}  ({i['secs']}+{o['secs']}s)", flush=True)
    out = {"sample": len(syms), "seed": a.seed, "start": a.start, "split": a.split, "end": end, "trials": len(rows) * 2, "rows": rows,
           "generated": time.strftime("%Y-%m-%d %H:%M")}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("写入", a.out, f"总耗时 {time.time() - t0:.0f}s")


def _prev(d: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _f(v):
    return "  —  " if v is None else f"{v:+.3f}"


if __name__ == "__main__":
    main()
