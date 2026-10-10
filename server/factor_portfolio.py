"""低风险多因子组合：定期调仓、等金额持有综合分最高的 N 只股票（不用止损止盈，调仓日按名单买卖）。

依据：A 股横截面单因子检验（约 1000 只 × 6 年、两组抽样、样本内外一致）——20 日平均换手率、ATR% 的 Rank IC 都稳定为负：
越冷门、越平稳的股票之后越强。组合把这两个「低风险」特征做成综合分，选前 N 名持有。
全市场回测（tools/factor_research.py，事先写好 8 个方案，2018~2022 选、2023~ 验）：样本内年化约 0~+1%（同期交易池等权 −5%、
沪深300 −1%），最大回撤约 −22%（等权 −43%）；样本外年化 +16~+21%，回撤约 −10%。「再加短期反转」的方案换手 17~30 倍、样本内更差，已否决。

口径（与回测引擎一致，无未来函数）：
  * 调仓日 T 收盘后用当日数据打分、出名单；T+1 开盘成交（滑点按不利方向）；涨停开盘买不进（本期放弃）、跌停开盘卖不出（顺延到下一日）
  * 只在当日交易池 L2 内选（日均成交额、价格、上市天数、停牌、ST 等过滤）
  * 换手缓冲：已持有的股票只要仍在前 2N 名就继续持有；新买入按「调仓日净值 ÷ N」等金额、整手向下取整
  * 费用：佣金（含最低 5 元）+ 印花税（卖出）+ 过户费；收益用复权价（含分红）
  * 调仓日历：从 anchor 起每 rebalance_days 个交易日一次——所有人、实盘和回测用同一套日历
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from datetime import datetime

import numpy as np
import pandas as pd

from . import db, execution, features as feats, market_calendar as mc, markets, settings, universe

log = logging.getLogger(__name__)
WARMUP = 120                     # 因子预热的交易日数：20 日换手、ATR(14) 的 Wilder 平滑、交易池的成交额过滤
INDEX_BENCH = (("sh.000300", "沪深300"), ("sh.000905", "中证500"))


def fp_cfg() -> dict:
    return settings.cfg()["factor_portfolio"]


def allocation() -> dict:
    """资金方案：宽基 ETF / 低风险组合 / 个股波段 三块的比例（个股波段 = 剩下的部分）。"""
    a = settings.cfg().get("allocation") or {}
    etf, fac = float(a.get("etf", 0) or 0), float(a.get("factor", 0) or 0)
    return {"etf": etf, "factor": fac, "swing": round(max(0.0, 1 - etf - fac), 4)}


# ---- 因子与综合分 ------------------------------------------------------------------------

def factor_frames(panel) -> dict[str, pd.DataFrame]:
    """只算组合需要的几个因子（比全量特征省内存，可以跑全市场 10 年）。"""
    close = panel.adj["close"].astype(np.float32)
    high, low = panel.adj["high"].astype(np.float32), panel.adj["low"].astype(np.float32)
    prev = close.ffill(limit=10).shift(1)
    tr = pd.DataFrame(np.fmax(np.fmax((high - low).to_numpy(), (high - prev).abs().to_numpy()), (low - prev).abs().to_numpy()),
                      index=close.index, columns=close.columns)
    atr14 = feats.wilder(tr, 14)
    return {"turnover_ma20": panel.turnover.astype(np.float32).rolling(20, min_periods=15).mean(),
            "atr_pct": atr14 / close,
            "ret_20": close / close.ffill(limit=10).shift(20) - 1}


def scores(F: dict[str, pd.DataFrame], l2: pd.DataFrame, method: str = "lowrisk") -> pd.DataFrame:
    """综合分 0~100（越高越优先），只在当日 L2 内排名；L2 之外为 NaN。
    lowrisk：低换手 + 低波动（默认）；lowrisk_rev：再加「近 20 日跌得多」（短期反转，回测已否决，只留作对照）。"""
    def lo(df):
        return 100 - df.where(l2).rank(axis=1, pct=True) * 100
    parts = [lo(F["turnover_ma20"]), lo(F["atr_pct"])]
    if method == "lowrisk_rev":
        parts.append(lo(F["ret_20"]))
    s = sum(parts) / len(parts)
    return s.where(l2)


def rank_order(vals: np.ndarray) -> np.ndarray:
    """按综合分从高到低的列下标（同分按列顺序，回测与实盘同一规则）。"""
    valid = np.where(~np.isnan(vals))[0]
    return valid[np.argsort(-vals[valid], kind="stable")]


def select_targets(order, held, n: int, buffer: float) -> tuple[list, dict]:
    """调仓规则：已持有且仍在前 buffer×N 名的继续拿着；其余名额按排名从高到低补满 N 只。返回 (目标名单, 名次)。"""
    rank = {j: r for r, j in enumerate(order, 1)}
    keep = sorted((j for j in held if rank.get(j, 10 ** 9) <= buffer * n), key=lambda j: rank[j])
    target, ks = list(keep), set(keep)
    for j in order:
        if len(target) >= n:
            break
        if j not in ks:
            target.append(j)
    return target, rank


# ---- 调仓日历 ----------------------------------------------------------------------------

def _anchor(anchor: str | None = None) -> str:
    return anchor or fp_cfg().get("anchor") or "2018-01-02"


def list_days(conn, start: str | None = None, end: str | None = None, every: int | None = None, anchor: str | None = None) -> list[str]:
    """调仓日（收盘后出名单的那天）：从 anchor 起第 0、every、2×every … 个交易日。"""
    every = int(every or fp_cfg()["rebalance_days"])
    days = mc.trading_days(conn, _anchor(anchor), end)
    return [d for i, d in enumerate(days) if i % every == 0 and (start is None or d >= start)]


def schedule(conn, day: str, every: int | None = None, anchor: str | None = None) -> dict:
    """day（数据截止日）在调仓日历里的位置：今天是不是调仓日、上一次 / 下一次是哪天、还有几个交易日。"""
    every = int(every or fp_cfg()["rebalance_days"])
    days = mc.trading_days(conn, _anchor(anchor), None)
    past = [d for d in days if d <= day]
    if not past:
        return {"every": every, "is_rebalance_day": False, "next_rebalance": None, "days_left": None}
    i = len(past) - 1
    k = i % every
    left = (every - k) % every
    j = i + left
    if j < len(days):
        nxt, est = days[j], False
    else:                                  # 交易日历只到今天：按工作日估算（节假日会让实际日期往后推）
        nxt, est = str(np.busday_offset(days[-1], j - len(days) + 1, roll="forward")), True
    exec_day = days[j + 1] if j + 1 < len(days) else str(np.busday_offset(nxt, 1, roll="forward"))
    return {"every": every, "anchor": _anchor(anchor), "is_rebalance_day": k == 0, "last_rebalance": past[i - k],
            "next_rebalance": nxt, "next_estimated": est, "days_left": left, "next_exec": exec_day}


# ---- 组合回测 ----------------------------------------------------------------------------

class Data:
    """一次加载、多次回测：面板、交易池、涨跌停标记、因子。
    methods 给定时按年分块加载（每块带 WARMUP 天预热），只保留回测要用的紧凑数组和这些综合分：
    全市场 8 年一次性加载峰值约 8.5 GB，分块后约 2 GB。复权价改用「不复权价 × 复权因子」（后复权）保证跨块连续，
    收益与按块内前复权完全一样（同一只股票只差一个常数倍）。"""

    def __init__(self, conn, start: str, end: str | None = None, market: str = "CN", symbols: list[str] | None = None,
                 methods: list[str] | None = None, chunk_days: int = 250):
        if methods:
            self._load_chunked(conn, start, end, market, symbols, methods, chunk_days)
            return
        from .panel import load_panel
        with settings.market_ctx(market):
            self.panel = load_panel(conn, start, end, market=market, symbols=symbols)
            boards, list_dates, self.names = universe.load_security_meta(conn)
            self.boards = boards
            self.l2, _, self.flags = universe.l2_mask(self.panel, boards, list_dates, return_parts=True, market=market)
            self.F = factor_frames(self.panel)
        P = self.panel
        self.market = market
        self.dates = list(P.dates)
        self.syms = list(P.symbols)
        f64 = lambda df: df.to_numpy(dtype=np.float64)  # noqa: E731
        self.O, self.C = f64(P.adj["open"]), f64(P.adj["close"])
        self.rO, self.rC = f64(P.raw["open"]), f64(P.raw["close"])
        self.status = P.status.to_numpy()
        self.ol_up = self.flags["open_limit_up"].to_numpy()
        self.ol_dn = self.flags["open_limit_down"].to_numpy()
        self.l2a = self.l2.to_numpy()
        self.board = [boards.get(s) for s in self.syms]
        self.minlot = np.array([execution.min_lot(b) for b in self.board], dtype=np.float64)
        self._score_cache: dict[str, np.ndarray] = {}

    def _load_chunked(self, conn, start, end, market, symbols, methods, chunk_days):
        from .panel import load_frame, panel_from_frame
        self.market = market
        self.panel = self.F = self.l2 = None
        with settings.market_ctx(market):
            boards, list_dates, self.names = universe.load_security_meta(conn)
            self.boards = boards
            all_days = mc.trading_days(conn, None, end)
            cal = np.asarray(all_days, dtype=str)
            min_list = int(markets.universe_cfg(market)["l2"]["min_list_days"])
            i0 = next((i for i, d in enumerate(all_days) if d >= start), len(all_days))
            parts: dict[str, list[pd.DataFrame]] = {}
            for a in range(i0, len(all_days), chunk_days):
                own = all_days[a: a + chunk_days]
                lo = all_days[max(i0, a - WARMUP)]
                df = load_frame(conn, lo, own[-1], symbols)
                if df.empty:
                    continue
                P = panel_from_frame(df)
                fac = df.loc[df["adj_factor"].notna(), ["symbol", "date", "adj_factor"]].sort_values("date").groupby("symbol")["adj_factor"].last()
                scale = fac.reindex(P.symbols).astype(np.float64)          # 块内前复权 × 块末因子 = 后复权（跨块连续）
                del df
                l2, cond, flags = universe.l2_mask(P, boards, list_dates, return_parts=True, market=market)
                l2 = _fix_list_age(l2, cond, P, list_dates, cal, min_list)
                F = factor_frames(P)
                keep = P.dates >= own[0]
                cut = lambda x: x.loc[keep]  # noqa: E731
                got = {"O": P.adj["open"].astype(np.float64).mul(scale, axis=1), "C": P.adj["close"].astype(np.float64).mul(scale, axis=1),
                       "rO": P.raw["open"].astype(np.float64), "rC": P.raw["close"].astype(np.float64), "status": P.status,
                       "ol_up": flags["open_limit_up"], "ol_dn": flags["open_limit_down"], "l2": l2}
                for m in methods:
                    got["s_" + m] = scores(F, l2, m)
                for k, v in got.items():
                    parts.setdefault(k, []).append(cut(v))
                del P, l2, flags, F, got
        cols = sorted(set().union(*[set(x.columns) for x in parts.get("C", [])])) if parts else []
        def cat(k, fill, dtype):
            x = pd.concat([f.reindex(columns=cols) for f in parts.pop(k)], axis=0)
            return (x if fill is None else x.fillna(fill)).to_numpy(dtype=dtype)
        if not parts:
            raise ValueError("回测区间内没有行情数据")
        dates = [d for x in parts["C"] for d in x.index]
        self.dates, self.syms = dates, cols
        self.O, self.C, self.rO, self.rC = (cat(k, None, np.float64) for k in ("O", "C", "rO", "rC"))
        self.status = cat("status", 0, np.int8)
        self.ol_up, self.ol_dn = cat("ol_up", False, bool), cat("ol_dn", False, bool)
        self.l2a = cat("l2", False, bool)
        self._score_cache = {m: cat("s_" + m, None, np.float64) for m in methods}
        self.board = [boards.get(s) for s in self.syms]
        self.minlot = np.array([execution.min_lot(b) for b in self.board], dtype=np.float64)

    def score(self, method: str) -> np.ndarray:
        if method not in self._score_cache:
            self._score_cache[method] = scores(self.F, self.l2, method).to_numpy(dtype=np.float64)
        return self._score_cache[method]


def _fix_list_age(l2: pd.DataFrame, cond: dict, P, list_dates: pd.Series, cal: np.ndarray, min_list: int) -> pd.DataFrame:
    """分块加载时，「上市满 N 个交易日」不能在块内数（预热只有 WARMUP 天，比 N 短）：
    已知上市日的，按全局交易日历算上市天数；未知的沿用块内口径。"""
    ldt = list_dates.reindex(P.symbols)
    known = ldt.notna().to_numpy()
    if not known.any():
        return l2
    pos_d = np.searchsorted(cal, np.asarray(P.dates, dtype=str))
    pos_l = np.searchsorted(cal, ldt.fillna("").astype(str).str[:10].to_numpy())
    age = pos_d[:, None] - pos_l[None, :] + 1
    ok = cond["list_age"].fillna(False).to_numpy(dtype=bool).copy()
    ok[:, known] = age[:, known] >= min_list
    rest = pd.DataFrame(True, index=P.dates, columns=P.symbols)
    for k, v in cond.items():
        if k != "list_age":
            rest &= v.fillna(False)
    return rest & pd.DataFrame(ok, index=P.dates, columns=P.symbols)


def rebalance_days(dates: list[str], start: str, every: int) -> set[int]:
    """从 start 起每 every 个交易日一次（含 start 当日）。"""
    i0 = next((i for i, d in enumerate(dates) if d >= start), len(dates))
    return set(range(i0, len(dates), every))


def run(D: Data, params: dict | None = None, start: str | None = None, end: str | None = None,
        equity0: float = 1_000_000, costs: dict | None = None, slippage: float | None = None,
        rb_dates: set[str] | None = None) -> dict:
    """组合回测。rb_dates 给定时按这些日期调仓（与实盘日历对齐），否则从 start 起每 rebalance_days 天一次。"""
    p = {**fp_cfg(), **(params or {})}
    n, every, method = int(p["n"]), int(p["rebalance_days"]), p["score"]
    buffer = float(p.get("buffer", 2.0))
    skip_unaffordable = bool(p.get("skip_unaffordable", True))
    slip = float(settings.cfg()["execution"]["slippage"] if slippage is None else slippage)
    costs = costs or (settings.cfg()["costs"]["cn"] if D.market == "CN" else settings.cfg()["costs"]["us"])
    S = D.score(method)
    dates = D.dates
    i_start = next((i for i, d in enumerate(dates) if d >= (start or dates[0])), 0)
    i_end = max(i for i, d in enumerate(dates) if d <= (end or dates[-1]))
    if rb_dates is not None:
        rb = {i for i, d in enumerate(dates) if d in rb_dates}
    else:
        rb = rebalance_days(dates, dates[i_start], every)
    cash = float(equity0)
    hold: dict[int, dict] = {}                      # j -> {units（复权份额）, shares, entry_t, cost}
    pend_sell: set[int] = set()
    pend_buy: dict[int, float] = {}                 # j -> 目标金额
    last_c = np.full(D.C.shape[1], np.nan)
    curve, trades = [], []
    fees_total = traded = 0.0
    n_rebalance = 0
    for t in range(i_start, i_end + 1):
        # 1) 开盘：先卖后买
        for j in list(pend_sell):
            if j not in hold:
                pend_sell.discard(j)
                continue
            if D.status[t, j] == 0 or D.ol_dn[t, j] or not (D.O[t, j] == D.O[t, j]):
                continue                             # 停牌 / 跌停开盘：卖不出，顺延
            h = hold.pop(j)
            proceeds_adj = h["units"] * D.O[t, j] * (1 - slip)
            fee = execution.trade_fees("sell", proceeds_adj / max(h["shares"], 1), h["shares"], costs, D.market)
            cash += proceeds_adj - fee
            fees_total += fee
            traded += proceeds_adj
            trades.append({"symbol": D.syms[j], "entry": dates[h["entry_t"]], "exit": dates[t],
                           "ret": proceeds_adj / h["cost"] - 1, "days": t - h["entry_t"]})
            pend_sell.discard(j)
        for j, amt in list(pend_buy.items()):
            if j in hold:
                pend_buy.pop(j)
                continue
            if D.status[t, j] == 0 or D.ol_up[t, j] or not (D.rO[t, j] == D.rO[t, j]) or D.rO[t, j] <= 0:
                pend_buy.pop(j)                      # 停牌 / 涨停开盘：买不进，本期放弃（不追）
                continue
            px = D.rO[t, j] * (1 + slip)
            shares = execution.lot_round(D.board[j], min(amt, cash) / px)
            while shares > 0 and shares * px + execution.trade_fees("buy", px, shares, costs, D.market) > cash:
                shares = execution.lot_round(D.board[j], shares - execution.lot_step(D.board[j]))
            pend_buy.pop(j)
            if shares <= 0:
                continue
            fee = execution.trade_fees("buy", px, shares, costs, D.market)
            cost = shares * px
            cash -= cost + fee
            fees_total += fee
            traded += cost
            s_t = D.rO[t, j] / D.O[t, j]             # 原始价 / 复权价：把股数换成复权份额，之后用复权价算市值（含分红）
            hold[j] = {"units": shares * s_t, "shares": shares, "entry_t": t, "cost": cost + fee}   # 复权份额 × 复权价 = 股数 × 原始价
        # 2) 收盘：市值
        c = D.C[t]
        ok = ~np.isnan(c)
        last_c[ok] = c[ok]
        mv = sum(h["units"] * last_c[j] for j, h in hold.items() if last_c[j] == last_c[j])
        equity = cash + mv
        curve.append((dates[t], equity, len(hold), mv))
        # 3) 调仓日收盘：出名单（T+1 开盘执行）
        if t in rb and t < i_end:
            order = rank_order(S[t])
            if not len(order):
                continue
            n_rebalance += 1
            per = equity / n
            if skip_unaffordable:              # 一手都买不起的（按今天收盘价估算）跳过，名额让给下一名——小资金不留现金缺口
                with np.errstate(invalid="ignore"):
                    afford = D.rC[t] * D.minlot * (1 + slip) <= per
                order = [int(j) for j in order if afford[j] or int(j) in hold]
            target, _ = select_targets([int(j) for j in order], list(hold), n, buffer)
            tset = set(target)
            pend_sell = {j for j in hold if j not in tset}
            pend_buy = {j: per for j in target if j not in hold}
    eq = pd.Series([x[1] for x in curve], index=[x[0] for x in curve])
    out = {"curve": eq, "holdings": pd.Series([x[2] for x in curve], index=eq.index),
           "invested": pd.Series([x[3] / x[1] if x[1] else 0.0 for x in curve], index=eq.index), "trades": trades,
           "fees": fees_total, "turnover": traded / max(float(eq.mean()), 1) / (len(eq) / 244) if len(eq) else 0.0,
           "final_holdings": [D.syms[j] for j in hold], "n_rebalance": n_rebalance, "params": p}
    out["metrics"] = metrics(eq)
    return out


def metrics(eq: pd.Series) -> dict:
    eq = eq.dropna()
    if len(eq) < 20:
        return {}
    r = eq.pct_change().dropna()
    yrs = len(r) / 244
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1
    vol = float(r.std() * math.sqrt(244))
    return {"from": eq.index[0], "to": eq.index[-1], "total": round(float(eq.iloc[-1] / eq.iloc[0] - 1), 4), "cagr": round(float(cagr), 4),
            "mdd": round(float((eq / eq.cummax() - 1).min()), 4), "vol": round(vol, 4),
            "sharpe": round(float(r.mean() * 244 / vol), 2) if vol else None}


def ew_benchmark(D: Data, start: str, end: str | None = None) -> pd.Series:
    """对照：当日交易池 L2 全部股票等权（每日再平衡、不计成本）。"""
    C = D.C
    with np.errstate(divide="ignore", invalid="ignore"):
        r = C[1:] / C[:-1] - 1
    m = D.l2a[:-1] & ~np.isnan(r)
    daily = np.where(m, r, 0).sum(1) / np.maximum(m.sum(1), 1)
    s = pd.Series(np.concatenate([[0.0], daily]), index=D.dates)
    s = s[(s.index >= start) & (s.index <= (end or s.index[-1]))]
    return (1 + s).cumprod()


def index_series(conn, symbol: str, start: str, end: str | None = None) -> pd.Series:
    q = "SELECT date, close FROM index_bar WHERE symbol=? AND date>=?" + (" AND date<=?" if end else "") + " ORDER BY date"
    df = pd.read_sql_query(q, conn, params=(symbol, start, end) if end else (symbol, start))
    return df.set_index("date")["close"].astype(float) if len(df) else pd.Series(dtype=float)


# ---- 回测报告（选股页「低风险组合」和回测页共用） -------------------------------------------

def _seg(s: pd.Series, lo: str | None = None, hi: str | None = None) -> pd.Series:
    if lo:
        s = s[s.index >= lo]
    if hi:
        s = s[s.index < hi]
    return s


def health_of(eq: pd.Series, years: float | None = None) -> dict:
    """策略健康度（与 ETF 规则同一口径）：当前回撤 vs 回测里的最大回撤。"""
    dd = eq / eq.cummax() - 1
    cur, worst = float(dd.iloc[-1]), float(dd.min())
    span = f"{years:.0f} 年" if years else "回测"
    if cur <= worst * 1.0001 and cur < 0:
        status, text = "警告", f"当前回撤已达到或超过 {span}回测里的最大回撤：出现了没见过的情况，建议暂停加钱、复查规则"
    elif worst < 0 and cur / worst >= 0.6:
        status, text = "注意", "当前回撤已到历史最大回撤的六成以上：属于回测里出现过的范围，按规则调仓，但别加大投入"
    else:
        status, text = "正常", "当前回撤在历史正常范围内"
    return {"status": status, "text": text, "current_dd": round(cur, 4), "max_dd": round(worst, 4), "max_dd_date": dd.idxmin(),
            "peak_date": eq.idxmax()}


def report(r: dict, benches: dict[str, pd.Series], oos_start: str | None = None, names: dict | None = None, step: int = 5) -> dict:
    eq = r["curve"]
    start, end = eq.index[0], eq.index[-1]
    split = oos_start if oos_start and start < oos_start <= end else None

    def segs(s):
        out = {"all": metrics(s)}
        if split:
            out["is"], out["oos"] = metrics(_seg(s, hi=split)), metrics(_seg(s, lo=split))
        return out

    bench = {k: v.reindex(eq.index).ffill().dropna() for k, v in benches.items() if v is not None and len(v)}
    bench = {k: v for k, v in bench.items() if len(v) >= 20}
    rows = [{"name": "低风险组合", "main": True, **segs(eq)}] + [{"name": k, **segs(v)} for k, v in bench.items()]

    def yret(s, y):
        a, prev = s[s.index.str.startswith(y)], s[s.index < y]
        if not len(a):
            return None, None
        base = prev.iloc[-1] if len(prev) else a.iloc[0]
        return round(float(a.iloc[-1] / base - 1), 4), round(float((a / np.maximum(a.cummax(), base) - 1).min()), 4)

    years = []
    for y in sorted({d[:4] for d in eq.index}):
        ret, dd = yret(eq, y)
        row = {"year": y, "ret": ret, "dd": dd, "partial": int((eq.index.str.startswith(y)).sum()) < 200, "bench": {}}
        for k, v in bench.items():
            row["bench"][k] = yret(v, y)[0]
        years.append(row)
    idx = list(range(0, len(eq), step)) + ([len(eq) - 1] if (len(eq) - 1) % step else [])
    curve = [{"date": eq.index[i], "port": round(float(eq.iloc[i] / eq.iloc[0]), 4),
              **{k: (round(float(v.get(eq.index[i]) / v.iloc[0]), 4) if eq.index[i] in v.index else None) for k, v in bench.items()}}
             for i in idx]
    tr = r["trades"]
    rets = np.array([t["ret"] for t in tr]) if tr else np.array([])
    yrs = len(eq) / 244
    names = {} if names is None else names
    return {
        "from": start, "to": end, "oos_start": split, "rows": rows, "by_year": years, "curve": curve, "bench_names": list(bench),
        "health": health_of(eq, yrs),
        "stats": {"turnover": round(float(r["turnover"]), 2), "fees": round(float(r["fees"]), 2),
                  "fees_per_year_pct": round(float(r["fees"]) / float(eq.mean()) / max(yrs, 1e-9), 4),
                  "avg_holdings": round(float(r["holdings"].mean()), 1), "invested": round(float(r["invested"].mean()), 3),
                  "n_trades": len(tr), "win_rate": round(float((rets > 0).mean()), 3) if len(rets) else None,
                  "avg_trade_ret": round(float(rets.mean()), 4) if len(rets) else None,
                  "avg_hold_days": round(float(np.mean([t["days"] for t in tr])), 1) if tr else None, "n_rebalance": r["n_rebalance"],
                  "equity0": round(float(eq.iloc[0]), 2), "equity1": round(float(eq.iloc[-1]), 2)},
        "final_holdings": [{"symbol": s, "name": names.get(s, s)} for s in r["final_holdings"]],
        "params": {k: r["params"].get(k) for k in ("score", "n", "rebalance_days", "buffer")},
    }


def _load_start(conn, start: str) -> str:
    days = mc.trading_days(conn, None, start)
    return days[max(0, len(days) - 1 - WARMUP)] if days else start


_load_lock = threading.Lock()                 # 全市场 10 年面板约 1.5 GB：同一时间只加载一份


def backtest(conn, market: str, params: dict | None = None, start: str | None = None, end: str | None = None,
             oos_start: str | None = None, equity: float = 100_000, align: bool = True) -> dict:
    """完整回测 + 报告。align=True 时调仓日与实盘日历对齐（从 anchor 起每 N 个交易日）。"""
    cfg = fp_cfg()
    p = {**cfg, **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
    asof = db.get_meta(conn, "data_asof") or conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    end = min(end or asof, asof)
    first = conn.execute("SELECT MIN(date) FROM daily_bar").fetchone()[0]
    days = [d for d in mc.trading_days(conn, first, end)]
    if len(days) < WARMUP + 60:
        return {"error": "insufficient", "message": f"行情数据不足（需要至少 {WARMUP + 60} 个交易日）"}
    start = max(start or cfg.get("backtest_start") or days[0], days[WARMUP])
    rb = None
    if align:
        lists = list_days(conn, start, end, every=int(p["rebalance_days"]))
        if not lists:
            return {"error": "insufficient", "message": "回测区间内没有调仓日"}
        start, rb = lists[0], set(lists)
    with _load_lock:
        D = Data(conn, _load_start(conn, start), end, market, methods=[p["score"]])
        r = run(D, p, start, end, equity, rb_dates=rb)
        benches = {"交易池等权": ew_benchmark(D, start, end)}
        names = D.names
        del D
    for sym, nm in INDEX_BENCH:
        benches[nm] = index_series(conn, sym, start, end)
    rep = report(r, benches, oos_start or cfg.get("oos_start"), names)
    rep.update(data_asof=asof, market=market, equity=equity)
    return rep


# ---- 选股页的回测摘要（后台算一次、存文件；数据日或参数变了才重算） ---------------------------

_state: dict[str, dict] = {}
_state_lock = threading.Lock()


def _cache_file(market: str):
    d = settings.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"factor_bt_{market}.json"


def bt_equity(sleeve: float | None) -> float:
    """摘要回测用的起始资金：取你分给组合的钱（按万取整，至少 2 万）——小资金的整手和最低 5 元佣金影响要算进去。"""
    return float(max(20000, round((sleeve or 30000) / 10000) * 10000))


def summary_key(asof: str, equity: float) -> str:
    c = fp_cfg()
    keys = ("score", "n", "rebalance_days", "buffer", "anchor", "backtest_start", "oos_start", "skip_unaffordable")
    raw = json.dumps({"asof": asof, "eq": equity, "p": {k: c.get(k) for k in keys}, "v": 3}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def compute_summary(market: str, equity: float, key: str) -> dict:
    with settings.market_ctx(market), db.market_db(market) as c:
        rep = backtest(c, market, equity=equity)
    rep["key"], rep["computed_at"] = key, datetime.now().isoformat(timespec="seconds")
    if not rep.get("error"):
        _cache_file(market).write_text(json.dumps(_clean(rep), ensure_ascii=False, default=str), encoding="utf-8")
    return rep


def compute_summary_isolated(market: str, equity: float, key: str, timeout: int = 1800) -> dict:
    """在独立的低优先级子进程里算回测摘要：全市场 8 年面板峰值约 2~3 GB，子进程结束后内存全部还给系统，
    不会叠加在后端常驻的选股面板（约 2 GB）上；也不抢界面的 CPU。返回 {"status": done|error, "message"}。"""
    import subprocess
    import sys
    flags = 0
    if os.name == "nt":
        flags = 0x00004000 | 0x08000000          # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW（双击启动时不弹黑窗口）
    try:
        r = subprocess.run([sys.executable, "-m", "server.factor_portfolio", "summary", market, str(equity), key],
                           cwd=str(settings.ROOT), env=os.environ.copy(), creationflags=flags, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "回测超时"}
    lines = [x for x in (r.stdout or "").splitlines() if x.startswith("{")]
    if r.returncode == 0 and lines:
        return json.loads(lines[-1])
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
    return {"status": "error", "message": " / ".join(tail)[-300:] or f"子进程退出码 {r.returncode}"}


def _read_cache(market: str) -> dict | None:
    f = _cache_file(market)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None


def get_summary(market: str, asof: str | None, sleeve: float | None, background: bool = True) -> dict:
    """读缓存；过期就在后台重算（约 1~2 分钟），先返回旧结果并标记 computing。"""
    equity = bt_equity(sleeve)
    key = summary_key(asof or "", equity)
    cached = _read_cache(market)
    if cached and cached.get("key") == key:
        return {**cached, "status": "ok"}
    with _state_lock:
        st = _state.get(market) or {}
        if st.get("key") == key and st.get("status") in ("computing", "error"):
            return {"status": st["status"], "message": st.get("message"), "stale": cached}
        if not background:
            return {"status": "missing", "stale": cached}
        _state[market] = {"key": key, "status": "computing"}

    def work():
        try:
            res = compute_summary_isolated(market, equity, key)
            with _state_lock:
                _state[market] = {"key": key, "status": res["status"], "message": res.get("message")}
        except Exception as e:  # noqa: BLE001
            log.exception("factor summary %s failed", market)
            with _state_lock:
                _state[market] = {"key": key, "status": "error", "message": f"{type(e).__name__}: {e}"}

    threading.Thread(target=work, name=f"factor-bt-{market}", daemon=True).start()
    return {"status": "computing", "stale": cached}


def refresh_summary(market: str = "CN", background: bool = False) -> str:
    """盘后任务 / 启动预热调用：数据日、参数或分给组合的资金变了就重算回测摘要。
    返回 cached（不用算）/ computing（别处正在算）/ done / error / no_data。"""
    from . import portfolio
    with db.market_db(market) as c:
        row = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()
        asof = db.get_meta(c, "data_asof") or (row[0] if row else None)
    if not asof:
        return "no_data"
    acct = portfolio.get_account(market)
    al = allocation()
    sleeve = acct["equity"] * al["factor"] if acct.get("equity") and al["factor"] else None
    if background:
        return get_summary(market, asof, sleeve)["status"]
    equity = bt_equity(sleeve)
    key = summary_key(asof, equity)
    cached = _read_cache(market)
    if cached and cached.get("key") == key:
        return "cached"
    with _state_lock:
        st = _state.get(market) or {}
        if st.get("key") == key and st.get("status") == "computing":
            return "computing"
        _state[market] = {"key": key, "status": "computing"}
    try:
        res = compute_summary_isolated(market, equity, key)
        status, msg = res["status"], res.get("message")
    except Exception as e:  # noqa: BLE001
        log.exception("factor summary %s failed", market)
        status, msg = "error", f"{type(e).__name__}: {e}"
    with _state_lock:
        _state[market] = {"key": key, "status": status, "message": msg}
    return status


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# ---- 实盘：今天的名单与明天的操作 ----------------------------------------------------------

def live_plan(conn, ctx: dict, acct: dict, positions: list[dict], market: str = "CN") -> dict:
    """用选股的全市场面板（数据截止日收盘）算今天的目标名单，与「组合」持仓比较得出卖 / 买 / 继续持有。"""
    cfg = fp_cfg()
    n, buffer, method = int(cfg["n"]), float(cfg.get("buffer", 2.0)), cfg["score"]
    F, l2, P = ctx["feat"].f, ctx["l2"], ctx["panel"]
    last = P.dates[-1]
    Fl = {k: F[k].loc[[last]] for k in ("turnover_ma20", "atr_pct", "ret_20") if k in F}
    srow = scores(Fl, l2.loc[[last]], method).iloc[0]
    syms = list(srow.index)
    order = [syms[j] for j in rank_order(srow.to_numpy(dtype=np.float64))]
    held_pos = [p for p in positions if (p.get("setup") or "") == "factor"]
    held = [p["symbol"] for p in held_pos]
    al = allocation()
    equity = acct.get("equity")
    sleeve = round(equity * al["factor"], 2) if equity and al["factor"] else None
    per = sleeve / n if sleeve else None
    slip = float(settings.cfg()["execution"]["slippage"])
    close = P.raw["close"].loc[last]
    skipped = []
    if per and cfg.get("skip_unaffordable", True):      # 一手都买不起的跳过、名额让给下一名（与回测同一规则）
        def afford(s):
            px = close.get(s)
            return px == px and px is not None and px * execution.min_lot(ctx["boards"].get(s)) * (1 + slip) <= per
        top = order[: n + len(held)]
        skipped = [s for s in top if s not in held and not afford(s)][:5]
        order = [s for s in order if s in held or afford(s)]
    target, rank = select_targets(order, held, n, buffer)
    tset = set(target)
    status = P.status.loc[last] if hasattr(P, "status") else None
    industry = ctx.get("industry")
    names = ctx["names"]

    def fval(k, s):
        v = F[k].at[last, s] if k in F and s in F[k].columns else np.nan
        return None if v != v else round(float(v), 5)

    def info(s):
        px = float(close.get(s, np.nan)) if s in close.index else float("nan")
        ind = industry.get(s) if industry is not None and len(industry) else None
        return {"symbol": s, "name": names.get(s, s), "industry": re.sub(r"^[A-Z]\d{2}", "", ind) if isinstance(ind, str) else None,
                "rank": rank.get(s), "score": None if srow.get(s) != srow.get(s) or srow.get(s) is None else round(float(srow[s]), 1),
                "close": None if px != px else round(px, 2), "turnover_ma20": fval("turnover_ma20", s), "atr_pct": fval("atr_pct", s),
                "suspended": bool(status is not None and s in status.index and int(status.get(s, 1) or 0) == 0)}

    targets = []
    for s in target:
        row = info(s)
        row["held"] = s in held
        if not row["held"] and per and row["close"]:
            board = ctx["boards"].get(s)
            sh = execution.lot_round(board, per / (row["close"] * (1 + slip)))
            row["shares"], row["amount"] = int(sh), round(sh * row["close"], 2)
            row["too_expensive"] = sh <= 0
        targets.append(row)
    pos_by = {p["symbol"]: p for p in held_pos}
    holdings = []
    for s in held:
        p = pos_by[s]
        row = info(s)
        px = row["close"]
        row.update(id=p["id"], qty=p["qty"], avg_cost=p["avg_cost"], open_date=p["open_date"], in_target=s in tset,
                   value=round(px * p["qty"], 2) if px else None, pnl_pct=round(px / p["avg_cost"] - 1, 4) if px and p["avg_cost"] else None)
        if not row["in_target"]:
            row["why"] = "已不在交易池（停牌 / ST / 成交额不足等）" if row["rank"] is None else f"排名跌到第 {row['rank']}（超过前 {int(buffer * n)} 名）"
        holdings.append(row)
    sched = schedule(conn, last)
    started = bool(held_pos)
    sells = [x for x in holdings if not x["in_target"]]
    buys = [x for x in targets if not x["held"]]
    if not started:
        mode = "start"
        head = f"还没建仓：随时可以开始——明天开盘按名单买入 {len(buys)} 只" + (f"（每只约 {per:,.0f} 元）" if per else "")
    elif sched["is_rebalance_day"]:
        mode = "rebalance"
        head = f"明天开盘调仓：卖出 {len(sells)} 只、买入 {len(buys)} 只" if (sells or buys) else "今天是调仓日，名单没有变化：不用操作"
    else:
        mode = "hold"
        head = f"持有 {len(held_pos)} 只，不用操作。下次调仓：{sched['next_rebalance']} 收盘后出名单（还有 {sched['days_left']} 个交易日）"
    return {"status": "ok", "as_of": last, "mode": mode, "headline": head, "schedule": sched,
            "params": {"score": method, "n": n, "rebalance_days": int(cfg["rebalance_days"]), "buffer": buffer},
            "allocation": {**al, "equity": equity, "sleeve": sleeve, "per_stock": None if per is None else round(per, 2)},
            "universe": int(l2.loc[last].sum()), "targets": targets, "holdings": holdings,
            "skipped_expensive": [{"symbol": s, "name": names.get(s, s), "close": round(float(close.get(s)), 2)} for s in skipped],
            "actions": {"sell": [x["symbol"] for x in sells] if mode != "hold" else [], "buy": [x["symbol"] for x in buys] if mode != "hold" else []},
            "held_value": round(sum(x["value"] or 0 for x in holdings), 2)}


if __name__ == "__main__":                     # 子进程入口：python -m server.factor_portfolio summary CN 30000 <key>
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "summary":
        logging.basicConfig(level=logging.WARNING)
        _rep = compute_summary(sys.argv[2], float(sys.argv[3]), sys.argv[4])
        print(json.dumps({"status": "error" if _rep.get("error") else "done", "message": _rep.get("message")}, ensure_ascii=False))
    else:
        print("用法：python -m server.factor_portfolio summary <CN> <起始资金> <缓存键>")
        sys.exit(2)
