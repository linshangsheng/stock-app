"""市场温度与指数 / ETF 择时（宽基指数的买入位、止损位、规则仓位）。

依据（2016-10 ~ 2026-10 A 股全市场日线实测，前 60% 样本内选参、后 40% 样本外检验，见 docs/使用手册.md）：
  * 市场级信号在 A 股是「逆向」的：全 A 站上 20 日线的比例 < 15%（恐慌 / 冰点）之后 20 日，宽基指数上涨概率约 61%~70%，
    明显高于其他区间；> 80%（过热）之后 60 日，沪深 300 上涨概率只有约 29%~36%。
  * 「趋势强就加仓、趋势弱就防守」的复合评分对未来 20 / 60 日收益没有预测力（秩相关 ≈ -0.05），故不采用。
  * 指数规则 = 50% 趋势仓（收盘站上 MA20×1.02 买、跌破 MA20×0.98 卖）+ 50% 抄底仓（全 A 站上 20 日线比例 < 15% 买，
    收盘跌破「信号日收盘 - 3×ATR20」止损，比例 > 80% 或持有满 40 个交易日离场）。4 个宽基平均：样本外年化 7.2%、
    最大回撤 -15%、Sharpe 0.63；同期持有不动 4.2% / -34% / 0.29。样本外每个指数只有约 15 笔抄底交易，统计不确定性大。
信号在收盘后产生、次日开盘执行（无未来函数）；所有阈值都在 config.yaml 的 market_view 段，可改、可回测。"""
from __future__ import annotations

import threading
from datetime import datetime

import numpy as np
import pandas as pd

from . import db, market_calendar as mc, settings

ZONES = [("冰点", "恐慌"), ("偏冷", ""), ("中性", ""), ("偏热", ""), ("过热", "情绪过热")]
_lock = threading.Lock()
_cache: dict = {}
_building: dict[str, bool] = {}


def mv_cfg() -> dict:
    return settings.cfg()["market_view"]


def index_list(market: str) -> list[dict]:
    c = mv_cfg()
    return list(c["indices_us"] if market == "US" else c["indices_cn"])


# ---- 全市场宽度（按股票分块读取，内存占用小；结果落库 market_breadth，之后只增量更新）----------------

def _chunk_counts(conn, symbols: list[str], start: str | None) -> pd.DataFrame:
    q = ("SELECT date, symbol, close*COALESCE(adj_factor,1) AS c, amount FROM daily_bar "
         "WHERE trade_status>0 AND symbol IN (%s)" % ",".join("?" * len(symbols)))
    args: list = list(symbols)
    if start:
        q += " AND date>=?"
        args.append(start)
    df = pd.read_sql_query(q, conn, params=args)
    if df.empty:
        return pd.DataFrame()
    C = df.pivot(index="date", columns="symbol", values="c").astype(np.float64).sort_index()
    A = df.pivot(index="date", columns="symbol", values="amount").reindex(C.index)
    valid = C.notna()
    out = {"n": valid.sum(1), "amount": A.sum(1)}
    for n in (20, 60, 200):
        m = C.rolling(n, min_periods=int(n * 0.8)).mean()
        ok = valid & m.notna()
        out[f"b{n}_num"] = ((C > m) & ok).sum(1)
        out[f"b{n}_den"] = ok.sum(1)
    r1 = (C / C.ffill(limit=10).shift(1) - 1).clip(-0.25, 0.25)
    out["up"], out["down"] = (r1 > 0).sum(1), (r1 < 0).sum(1)
    out["r_sum"], out["r_cnt"] = r1.sum(1), r1.notna().sum(1)
    hi, lo = C.rolling(250, min_periods=200).max(), C.rolling(250, min_periods=200).min()
    out["nh"], out["nl"] = ((C >= hi) & valid & hi.notna()).sum(1), ((C <= lo) & valid & lo.notna()).sum(1)
    return pd.DataFrame(out)


def compute_breadth(conn, start: str | None = None, chunk: int = 800) -> pd.DataFrame:
    """全市场（库内全部股票，含已退市；停牌日不计）每日宽度：站上 MA20/60/200 的比例、涨跌家数、新高新低、等权日收益。"""
    syms = [r[0] for r in conn.execute("SELECT symbol FROM securities ORDER BY symbol")]
    acc: pd.DataFrame | None = None
    for i in range(0, len(syms), chunk):
        part = _chunk_counts(conn, syms[i:i + chunk], start)
        if part.empty:
            continue
        acc = part if acc is None else acc.add(part, fill_value=0)
    if acc is None:
        return pd.DataFrame()
    res = pd.DataFrame(index=acc.index)
    res["n"] = acc["n"].astype(int)
    for n in (20, 60, 200):
        res[f"b{n}"] = acc[f"b{n}_num"] / acc[f"b{n}_den"].replace(0, np.nan)
    res["up"], res["down"] = acc["up"].astype(int), acc["down"].astype(int)
    res["nh"], res["nl"] = acc["nh"].astype(int), acc["nl"].astype(int)
    res["ew_ret"] = acc["r_sum"] / acc["r_cnt"].replace(0, np.nan)
    res["amount"] = acc["amount"]
    return res


def ensure_breadth(conn, market: str = "CN") -> int:
    """首次全量计算（约 1~2 分钟），之后每次只重算最近 ~320 个交易日、写回最近 10 个交易日（吸收快照对账的修正）。"""
    last = conn.execute("SELECT MAX(date) FROM market_breadth").fetchone()[0]
    data_last = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    if not data_last:
        return 0
    if last and last >= data_last and conn.execute("SELECT 1 FROM market_breadth WHERE date=?", (data_last,)).fetchone():
        return 0
    if last:
        days = mc.trading_days(conn, None, data_last)
        k = days.index(last) if last in days else len(days) - 1
        start = days[max(0, k - 330)]
        write_from = days[max(0, k - 10)]
    else:
        start = write_from = None
    df = compute_breadth(conn, start)
    if df.empty:
        return 0
    if write_from:
        df = df[df.index >= write_from]
    df = df[df["b20"].notna()]
    rows = [(d, int(r.n), _nz(r.b20), _nz(r.b60), _nz(r.b200), int(r.up), int(r.down), int(r.nh), int(r.nl), _nz(r.ew_ret), _nz(r.amount))
            for d, r in df.iterrows()]
    conn.executemany("INSERT OR REPLACE INTO market_breadth(date,n,b20,b60,b200,up,down,nh,nl,ew_ret,amount) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    _cache.clear()
    return len(rows)


def _nz(v):
    return None if v is None or v != v else float(v)


def load_breadth(conn) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM market_breadth ORDER BY date", conn).set_index("date")


def zone_of(b20: float | None) -> int | None:
    if b20 is None or b20 != b20:
        return None
    for i, cut in enumerate(mv_cfg()["zones"]):
        if b20 < cut:
            return i
    return len(ZONES) - 1


# ---- 指数规则（50% 趋势仓 + 50% 抄底仓）----------------------------------------------------------

def atr(px: pd.DataFrame, n: int = 20) -> pd.Series:
    pc = px["close"].shift(1)
    tr = pd.concat([px["high"] - px["low"], (px["high"] - pc).abs(), (px["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def simulate(px: pd.DataFrame, b20: pd.Series, rule: dict | None = None) -> dict:
    """逐日模拟规则仓位（收盘决定，次日开盘执行）。px: index=date，含 open/high/low/close。"""
    rule = rule or mv_cfg()["index_rule"]
    tr_c, wo = rule["trend"], rule["washout"]
    c = px["close"].to_numpy(dtype=float)
    o = px["open"].to_numpy(dtype=float)
    ma = px["close"].rolling(tr_c["ma"], min_periods=tr_c["ma"]).mean().to_numpy()
    a = atr(px, wo.get("atr_n", 20)).to_numpy()
    b = b20.reindex(px.index).to_numpy(dtype=float)
    dates = list(px.index)
    T = len(c)
    pt, pw = np.zeros(T), np.zeros(T)
    trades: list[dict] = []
    t_on, w_on = False, False
    t_open: dict = {}
    w_open: dict = {}

    def _close(sleeve: str, info: dict, t: int, reason: str):
        ex_px = o[t + 1] if t + 1 < T else None
        trades.append({"sleeve": sleeve, "signal_date": info["signal_date"], "entry_date": info["entry_date"], "entry": info["entry"],
                       "exit_signal_date": dates[t], "exit_date": dates[t + 1] if t + 1 < T else None, "exit": ex_px, "reason": reason,
                       "ret": (ex_px / info["entry"] - 1) if ex_px and info["entry"] else None, "stop": info.get("stop")})

    for t in range(T):
        # 趋势仓：带 ±band 滞回，减少来回打脸
        if not np.isnan(ma[t]):
            if not t_on and c[t] > ma[t] * (1 + tr_c["band"]):
                t_on = True
                t_open = {"signal_date": dates[t], "entry_date": dates[t + 1] if t + 1 < T else None, "entry": o[t + 1] if t + 1 < T else None}
            elif t_on and c[t] < ma[t] * (1 - tr_c["band"]):
                t_on = False
                _close("trend", t_open, t, f"收盘跌破 MA{tr_c['ma']}×{1 - tr_c['band']:.2f}")
        # 抄底仓：全 A 宽度冰点买入，止损 / 宽度过热 / 持有期满离场
        if w_on:
            held = t - w_open["t"]
            reason = None
            tp = _wash_tp(w_open, wo)
            if c[t] < w_open["stop"]:
                reason = "收盘跌破止损价"
            elif tp and held >= 1 and c[t] >= tp:
                reason = "收盘达到短期止盈位"
            elif not np.isnan(b[t]) and b[t] > wo["exit_above"]:
                reason = f"宽度 > {wo['exit_above']:.0%}（情绪过热）止盈"
            elif held >= wo["max_hold_days"]:
                reason = f"持有满 {wo['max_hold_days']} 个交易日"
            if reason:
                w_on = False
                _close("washout", w_open, t, reason)
        elif not np.isnan(b[t]) and b[t] < wo["enter_below"] and not np.isnan(a[t]):
            w_on = True
            w_open = {"t": t, "signal_date": dates[t], "entry_date": dates[t + 1] if t + 1 < T else None,
                      "entry": o[t + 1] if t + 1 < T else None, "stop": c[t] - wo["stop_atr_k"] * a[t], "signal_close": c[t],
                      "signal_atr": a[t]}
        pt[t], pw[t] = float(t_on), float(w_on)
    pos = tr_c["weight"] * pt + wo["weight"] * pw
    return {"pos": pd.Series(pos, index=px.index), "trend": pd.Series(pt, index=px.index), "washout": pd.Series(pw, index=px.index),
            "trades": trades, "open": {"trend": t_open if t_on else None, "washout": w_open if w_on else None},
            "ma": pd.Series(ma, index=px.index), "atr": pd.Series(a, index=px.index)}


def _wash_tp(w_open: dict, wo: dict) -> float | None:
    """抄底仓短期止盈位：买入价（次日开盘）×(1+pct)，或 信号日收盘 + k×ATR；都为 0 时不设。"""
    base = w_open.get("entry") or w_open.get("signal_close")
    if not base:
        return None
    if wo.get("take_profit_pct"):
        return base * (1 + wo["take_profit_pct"])
    if wo.get("take_profit_atr") and w_open.get("signal_atr"):
        return w_open["signal_close"] + wo["take_profit_atr"] * w_open["signal_atr"]
    return None


def daily_returns(px: pd.DataFrame, pos: pd.Series, cost: float) -> pd.Series:
    """pos[t] 为收盘 t 决定的目标仓位，t+1 开盘成交：前一段按 开盘/昨收、后一段按 收盘/开盘 计。"""
    o, c = px["open"], px["close"]
    p_exec, p_prev = pos.shift(1).fillna(0), pos.shift(2).fillna(0)
    r = p_prev * (o / c.shift(1) - 1) + p_exec * (c / o - 1) - (p_exec - p_prev).abs() * cost
    return r.fillna(0)


def perf(r: pd.Series, pos: pd.Series | None = None) -> dict:
    if len(r) < 60:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / 244
    vol = float(r.std() * np.sqrt(244))
    mdd = float((eq / eq.cummax() - 1).min())
    cagr = float(eq.iloc[-1] ** (1 / yrs) - 1)
    out = {"from": r.index[0], "to": r.index[-1], "cagr": round(cagr, 4), "mdd": round(mdd, 4), "vol": round(vol, 4),
           "sharpe": round(float(r.mean() * 244 / vol), 2) if vol else None, "total": round(float(eq.iloc[-1] - 1), 4)}
    if pos is not None:
        out["exposure"] = round(float(pos.mean()), 3)
    return out


def _index_px(conn, symbol: str) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT date, open, high, low, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(symbol,))
    df = df.set_index("date").astype(float)
    df["open"] = df["open"].where(df["open"] > 0, df["close"])          # 个别指数早期无开盘价：用收盘价代替
    return df.dropna(subset=["close"])


def analyze_index(conn, item: dict, br: pd.DataFrame) -> dict | None:
    px = _index_px(conn, item["symbol"])
    if len(px) < 120:
        return None
    c = mv_cfg()
    rule = c["index_rule"]
    sim = simulate(px, br["b20"], rule)
    last = px.index[-1]
    cl = float(px["close"].iloc[-1])
    ma = float(sim["ma"].iloc[-1])
    a = float(sim["atr"].iloc[-1])
    b_now = float(br["b20"].reindex(px.index).iloc[-1]) if last in br.index else None
    tr_c, wo = rule["trend"], rule["washout"]
    hi52 = float(px["high"].tail(250).max())

    def ret(n):
        return float(cl / px["close"].iloc[-1 - n] - 1) if len(px) > n else None

    # 趋势仓
    t_open = sim["open"]["trend"]
    trend = {"holding": t_open is not None, "ma": ma, "buy_level": ma * (1 + tr_c["band"]), "exit_level": ma * (1 - tr_c["band"])}
    if t_open:
        trend.update(since=t_open["signal_date"], entry=t_open["entry"],
                     dist_exit=cl / trend["exit_level"] - 1,
                     text=f"持有中：收盘跌破 {trend['exit_level']:.0f}（MA{tr_c['ma']}×{1 - tr_c['band']:.2f}，距今 {cl / trend['exit_level'] - 1:+.1%}）则次日开盘卖出")
    else:
        trend.update(dist_buy=trend["buy_level"] / cl - 1,
                     text=f"空仓：收盘站上 {trend['buy_level']:.0f}（MA{tr_c['ma']}×{1 + tr_c['band']:.2f}，距今 {trend['buy_level'] / cl - 1:+.1%}）则次日开盘买入")
    # 抄底仓
    w_open = sim["open"]["washout"]
    wash = {"holding": w_open is not None, "enter_below": wo["enter_below"], "exit_above": wo["exit_above"], "b20": b_now}
    if w_open:
        held = len(px) - 1 - w_open["t"]
        refs = [{"pct": p, "price": round(w_open["entry"] * (1 + p), 2)} for p in wo.get("ref_take_profit", [])] if w_open.get("entry") else []
        wash.update(ref_take_profit=refs, rule_take_profit=_wash_tp(w_open, wo))
        wash.update(since=w_open["signal_date"], entry=w_open["entry"], stop=w_open["stop"], days_held=held,
                    days_left=max(0, wo["max_hold_days"] - held), dist_stop=cl / w_open["stop"] - 1,
                    text=f"持有中（{w_open['signal_date']} 恐慌信号买入）：止损 {w_open['stop']:.0f}（距今 {cl / w_open['stop'] - 1:+.1%}）；"
                         f"全A站上20日线比例 > {wo['exit_above']:.0%} 止盈，最多再持有 {max(0, wo['max_hold_days'] - held)} 个交易日")
    else:
        wash.update(ref_take_profit=[{"pct": p, "price": round(cl * (1 + p), 2)} for p in wo.get("ref_take_profit", [])])
        stop_if = cl - wo["stop_atr_k"] * a
        wash.update(stop_if_today=stop_if, stop_pct_if_today=stop_if / cl - 1,
                    text=f"等待：全A站上20日线比例跌破 {wo['enter_below']:.0%}（当前 {b_now:.0%}）才买入；若今天触发，止损约 {stop_if:.0f}（{stop_if / cl - 1:+.1%}）"
                    if b_now is not None else "等待宽度数据")
    pos_now = float(sim["pos"].iloc[-1])
    # 明天开盘要做什么：只由「今天收盘」的信号决定，与明天高开 / 低开无关
    def _chg(series, name, wt):
        a_, b_ = float(series.iloc[-1]), float(series.iloc[-2]) if len(series) > 1 else 0.0
        if a_ > b_:
            return f"开盘买入{name}（{wt:.0%}）"
        if a_ < b_:
            return f"开盘卖出{name}"
        return None
    acts = [x for x in (_chg(sim["trend"], "趋势仓", tr_c["weight"]), _chg(sim["washout"], "抄底仓", wo["weight"])) if x]
    # 明天收盘价的触发点位：明天的 MA 含明天收盘 C，C > (1+band)·(S + C)/n  ⇒  C > (1+band)·S / (n − (1+band))，S = 最近 n−1 日收盘之和
    n_ma = tr_c["ma"]
    s_prev = float(px["close"].tail(n_ma - 1).sum())
    buy_close = (1 + tr_c["band"]) * s_prev / (n_ma - (1 + tr_c["band"]))
    exit_close = (1 - tr_c["band"]) * s_prev / (n_ma - (1 - tr_c["band"]))
    tmr = []
    if acts:
        tmr.append({"when": "明天开盘", "level": "高开 / 低开多少都一样", "then": "；".join(acts) + "（按开盘价成交）"})
    else:
        tmr.append({"when": "明天开盘", "level": "高开 / 低开多少都一样", "then": "不操作：规则只看收盘，开盘跳空不改变信号"})
    if t_open:
        tmr.append({"when": "明天收盘", "level": f"< {exit_close:.0f}（较今收 {exit_close / cl - 1:+.1%}）", "then": "趋势仓卖出信号 → 后天开盘卖"})
    else:
        tmr.append({"when": "明天收盘", "level": f"≥ {buy_close:.0f}（较今收 {buy_close / cl - 1:+.1%}）", "then": f"趋势仓买入信号 → 后天开盘买（{tr_c['weight']:.0%}）"})
    if w_open:
        tmr.append({"when": "明天收盘", "level": f"< {w_open['stop']:.0f}（较今收 {w_open['stop'] / cl - 1:+.1%}）", "then": "抄底仓止损 → 后天开盘卖"})
        tmr.append({"when": "明天收盘", "level": f"全A站上20日线比例 > {wo['exit_above']:.0%}", "then": "抄底仓止盈 → 后天开盘卖"})
    else:
        tmr.append({"when": "明天收盘", "level": f"全A站上20日线比例 < {wo['enter_below']:.0%}（今天 {b_now:.0%}）" if b_now is not None else "宽度数据缺失",
                    "then": f"抄底仓买入信号 → 后天开盘买（{wo['weight']:.0%}），止损约 {cl - wo['stop_atr_k'] * a:.0f}"})
    tmr.append({"when": "盘中", "level": "跌破止损 / 离场位", "then": "先不卖，等收盘确认（规则只看收盘价）"})
    gaps = (px["open"] / px["close"].shift(1) - 1).tail(500).dropna()
    next_open = {"actions": acts, "tomorrow": tmr, "buy_close": round(buy_close, 2), "exit_close": round(exit_close, 2), "text": ("明天：" + "；".join(acts) + "，不论高开低开都按开盘价成交") if acts else "明天：没有买卖信号，不论高开低开都不用操作",
                 "gap_median": round(float(gaps.abs().median()), 4) if len(gaps) else None,
                 "gap_gt1": round(float((gaps.abs() > 0.01).mean()), 4) if len(gaps) else None}
    # 回测：全样本 / 样本内 / 样本外，规则 vs 持有不动
    cost = rule["cost_per_side"]
    valid_from = br.index[br["b20"].notna()][0] if br["b20"].notna().any() else px.index[0]
    start_i = max(int(px.index.searchsorted(valid_from)), tr_c["ma"], 60)
    p = px.iloc[start_i:]
    r_rule = daily_returns(p, sim["pos"].iloc[start_i:], cost)
    r_hold = daily_returns(p, pd.Series(1.0, index=p.index), cost)
    cut = p.index[int(len(p) * 0.6)]
    stats = {}
    for part, m in (("all", slice(None)), ("is", p.index < cut), ("oos", p.index >= cut)):
        stats[part] = {"rule": perf(r_rule[m], sim["pos"].iloc[start_i:][m]), "hold": perf(r_hold[m])}
    closed = [t for t in sim["trades"] if t["ret"] is not None]
    wins = [t for t in closed if t["sleeve"] == "washout"]
    stats["washout_trades"] = {"n": len(wins), "win_rate": round(sum(1 for t in wins if t["ret"] > 0) / len(wins), 3) if wins else None,
                               "avg_ret": round(float(np.mean([t["ret"] for t in wins])), 4) if wins else None}
    return {**item, "date": last, "close": cl, "chg_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20), "ret_60d": ret(60),
            "atr": a, "atr_pct": a / cl, "dd_52w": cl / hi52 - 1, "above_ma60": cl > float(px["close"].tail(60).mean()),
            "above_ma250": cl > float(px["close"].tail(250).mean()) if len(px) >= 250 else None,
            "trend": trend, "washout": wash, "position": pos_now, "next_open": next_open,
            "position_text": {0.0: "规则仓位 0%：空仓等待", 0.5: "规则仓位 50%", 1.0: "规则仓位 100%（趋势仓 + 抄底仓）"}.get(round(pos_now, 2), f"规则仓位 {pos_now:.0%}"),
            "stats": stats, "recent_trades": sim["trades"][-6:][::-1],
            "curve": _curve(r_rule, r_hold)}


def _curve(r_rule: pd.Series, r_hold: pd.Series, step: int = 5) -> list[dict]:
    a, b = (1 + r_rule).cumprod(), (1 + r_hold).cumprod()
    idx = list(range(0, len(a), step)) + ([len(a) - 1] if (len(a) - 1) % step else [])
    return [{"date": a.index[i], "rule": round(float(a.iloc[i]), 4), "hold": round(float(b.iloc[i]), 4)} for i in idx]


# ---- 历史证据：各温度区间之后 N 日的涨跌 -----------------------------------------------------------

def zone_evidence(conn, br: pd.DataFrame, market: str) -> dict:
    main = mv_cfg()["evidence_index_us" if market == "US" else "evidence_index_cn"]
    px = _index_px(conn, main)["close"].reindex(br.index).ffill()
    ew = (1 + br["ew_ret"].fillna(0)).cumprod()
    z = br["b20"].map(zone_of)
    out = {"index": main, "horizons": {}}
    for h in (20, 60):
        d = pd.DataFrame({"z": z, "idx": px.shift(-h) / px - 1, "ew": ew.shift(-h) / ew - 1}).dropna()
        rows = []
        for i, (name, _) in enumerate(ZONES):
            g = d[d["z"] == i]
            rows.append({"zone": i, "name": name, "days": int(len(g)),
                         "idx_mean": _r(g["idx"].mean()), "idx_win": _r((g["idx"] > 0).mean()),
                         "ew_mean": _r(g["ew"].mean()), "ew_win": _r((g["ew"] > 0).mean())})
        out["horizons"][str(h)] = rows
    out["from"], out["to"] = (br.index[0], br.index[-1]) if len(br) else (None, None)
    return out


def _r(v, d=4):
    return None if v is None or v != v else round(float(v), d)


# ---- 汇总 -------------------------------------------------------------------------------------

def thermometer(br: pd.DataFrame, ev: dict) -> dict:
    last = br.iloc[-1]
    b20 = float(last["b20"])
    z = zone_of(b20)
    cuts = mv_cfg()["zones"]
    tail10 = br.tail(10)
    ew = (1 + br["ew_ret"].fillna(0)).cumprod()
    e20 = {r["zone"]: r for r in ev["horizons"]["20"]}
    e60 = {r["zone"]: r for r in ev["horizons"]["60"]}
    zr, zr60 = e20.get(z, {}), e60.get(z, {})
    hint = {
        0: "市场恐慌、普跌之后：历史上这是宽基指数 ETF 最好的分批买入区，抄底仓信号已触发（务必设止损）。",
        1: "市场偏冷：抄底条件尚未满足（需跌破 {lo:.0%}）；趋势仓按各指数是否站上 MA20 判断。",
        2: "情绪中性：没有明显优势，按各指数的趋势仓信号执行，不加额外仓位。",
        3: "情绪偏热：已在赚钱效应中，可以持有，但新开仓宜小、不追高。",
        4: "情绪过热：历史上之后 60 天上涨概率明显偏低。抄底仓止盈；不追高，新开仓宜小。",
    }[z].format(lo=cuts[0])
    return {
        "date": br.index[-1], "b20": b20, "b60": _r(last["b60"]), "b200": _r(last["b200"]), "zone": z, "zone_name": ZONES[z][0], "cuts": cuts,
        "up": int(last["up"]), "down": int(last["down"]), "pool": int(last["n"]),
        "adv_ratio_10d": _r(tail10["up"].sum() / max(1, (tail10["up"] + tail10["down"]).sum())),
        "net_new_highs": int(last["nh"]) - int(last["nl"]), "new_highs": int(last["nh"]), "new_lows": int(last["nl"]),
        "ew_ret_20d": _r(ew.iloc[-1] / ew.iloc[-21] - 1) if len(ew) > 21 else None,
        "ew_dd_250d": _r(ew.iloc[-1] / ew.tail(250).max() - 1),
        "amount_ratio": _r(br["amount"].tail(5).mean() / br["amount"].tail(60).mean()) if len(br) > 60 else None,
        "hint": hint,
        "evidence_line": (f"历史上处在「{ZONES[z][0]}」之后：20 天{_idx_name(ev['index'])}平均 {zr.get('idx_mean', 0):+.1%}、上涨概率 {zr.get('idx_win', 0):.0%}；"
                          f"60 天平均 {zr60.get('idx_mean', 0):+.1%}、上涨概率 {zr60.get('idx_win', 0):.0%}（{zr.get('days', 0)} 个交易日样本）") if zr else "",
        "history": [{"date": d, "b20": _r(v, 3)} for d, v in br["b20"].tail(250).items()],
    }


def _idx_name(sym: str) -> str:
    for it in mv_cfg()["indices_cn"] + mv_cfg()["indices_us"]:
        if it["symbol"] == sym:
            return it["name"]
    return sym


def rotation(items: list[dict]) -> list[dict]:
    """风格强弱：按 20 日涨跌排序（大小盘 / 成长价值的轮动参考）。"""
    rows = [{"name": i["name"], "style": i.get("style"), "ret_20d": i["ret_20d"], "ret_60d": i["ret_60d"]} for i in items if i.get("ret_20d") is not None]
    return sorted(rows, key=lambda x: -x["ret_20d"])


def build(market: str) -> dict:
    with settings.market_ctx(market), db.market_db(market) as conn:
        asof = db.get_meta(conn, "data_asof") or conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        if not asof:
            return {"status": "no_data", "message": "尚无行情数据，请先初始化数据"}
        key = (market, asof, conn.execute("SELECT MAX(date) FROM index_bar").fetchone()[0], settings.config_hash(mv_cfg()))
        with _lock:
            if _cache.get(market, {}).get("key") == key:
                return _cache[market]["data"]
        last_b = conn.execute("SELECT MAX(date) FROM market_breadth").fetchone()[0]
        if not last_b or last_b < asof:
            start_background(market)
            if not last_b:
                return {"status": "computing", "message": "正在计算全市场宽度历史（首次约 1~2 分钟），请稍后刷新"}
        br = load_breadth(conn)
        ev = zone_evidence(conn, br, market)
        items = [x for x in (analyze_index(conn, it, br) for it in index_list(market)) if x]
        data = {"status": "ok", "date": br.index[-1], "data_asof": asof, "thermometer": thermometer(br, ev), "evidence": ev,
                "indices": items, "rotation": rotation(items), "rule": mv_cfg()["index_rule"],
                "computed_at": datetime.now().isoformat(timespec="seconds"),
                "note": "规则输出，不是投资建议。阈值来自 A 股 2016~2026 年数据的样本内选参、样本外检验；样本外抄底交易笔数少，统计不确定性大。"
                        + ("美股使用同一套阈值，未经美股数据单独验证。" if market == "US" else "")}
        with _lock:
            _cache[market] = {"key": key, "data": data}
        return data


def start_background(market: str) -> None:
    """后台补齐宽度表（首次全量较慢，不阻塞请求）。"""
    if _building.get(market):
        return
    _building[market] = True

    def run():
        try:
            with settings.market_ctx(market), db.market_db(market) as conn:
                ensure_breadth(conn, market)
        finally:
            _building[market] = False
    threading.Thread(target=run, name=f"breadth-{market}", daemon=True).start()
