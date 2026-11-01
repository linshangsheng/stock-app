"""指数筹码分布（估算）。仅 A 股：美股的日线没有换手率，不做。

筹码分布不是真实数据——交易所没有「谁在什么价位持仓」的记录。同花顺等软件的做法是用每天的价格区间和换手率推算，本模块用同样的思路：
  1. 每天的成交筹码按「三角形」摊在当天的最低价~最高价之间（峰值在 (最高+最低+2×收盘)/4）；
  2. 之前所有筹码按当天换手率衰减：新分布 = 旧分布 × (1 − 换手率) + 换手率 × 当天的三角形（总量恒为 1）；
  3. 把几百天叠起来，就是某一天收盘后的筹码分布。
指数没有自己的换手率，用成分股汇总得到：指数换手率 = Σ成交额 / Σ流通市值，其中每只股票的流通市值 = 成交额 ÷ 换手率（库里没有流通股本，只能这样反推）。
成分股没有官方名单可用，按「流通市值最大的 N 只」近似（上证50 = 沪市主板前 50；沪深300 = 全市场前 300；创业板指 = 创业板前 100），
并对整段历史使用同一批成分股。这是估算：形状与同花顺相近，具体数字不会一一对应。展示用，不进入选股与回测。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

NBINS = 200                      # 价格格数
GRID_DAYS = 750                  # 价格网格覆盖最近多少个交易日的最高 / 最低价（更早、更远的筹码并入边缘格，早已衰减到可忽略）
TR_MIN, TR_MAX = 0.0005, 1.0     # 换手率（小数）的合理范围

# 成分股近似：scope = 在哪些股票里选；n = 取流通市值最大的几只
SCOPES = {
    "sh": "AND b.symbol LIKE 'sh.6%' AND b.symbol NOT LIKE 'sh.688%'",     # 沪市主板（不含科创板）
    "chinext": "AND s.board='chinext'",
    "all": "",
}


def members(conn, scope: str, n: int) -> list[str]:
    """流通市值最大的 n 只在市股票（最近约 30 个交易日的平均流通市值 = 成交额 ÷ 换手率）。"""
    last = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    if not last:
        return []
    start = (date.fromisoformat(last) - timedelta(days=45)).isoformat()
    rows = conn.execute(
        "SELECT b.symbol, AVG(b.amount / (b.turnover / 100.0)) AS cap FROM daily_bar b JOIN securities s ON s.symbol = b.symbol "
        f"WHERE b.date >= ? AND b.turnover > 0 AND b.amount > 0 AND b.trade_status = 1 AND s.status = 'active' {SCOPES[scope]} "
        "GROUP BY b.symbol ORDER BY cap DESC LIMIT ?", (start, n)).fetchall()
    return [r[0] for r in rows]


def index_turnover(conn, syms: list[str], start: str) -> pd.Series:
    """成分股汇总的指数换手率（小数，如 0.0103 = 1.03%）：Σ成交额 / Σ（成交额 ÷ 换手率）。"""
    if not syms:
        return pd.Series(dtype=float)
    ph = ",".join("?" * len(syms))
    rows = conn.execute(
        f"SELECT date, SUM(amount), SUM(amount / (turnover / 100.0)) FROM daily_bar WHERE symbol IN ({ph}) AND date >= ? "
        "AND turnover > 0 AND amount > 0 GROUP BY date ORDER BY date", [*syms, start]).fetchall()
    return pd.Series({d: a / c for d, a, c in rows if c}, dtype=float)


def _triangle(ctr: np.ndarray, lo: float, hi: float, peak: float) -> np.ndarray:
    """三角形分布在各价格格上的权重（和为 1）。区间窄到一格都放不下时，全部放在收盘价所在的格。"""
    inside = (ctr >= lo) & (ctr <= hi)
    if not inside.any() or hi <= lo:
        w = np.zeros_like(ctr)
        w[int(np.argmin(np.abs(ctr - peak)))] = 1.0
        return w
    up = np.where(peak > lo, (ctr - lo) / max(peak - lo, 1e-12), 1.0)
    dn = np.where(hi > peak, (hi - ctr) / max(hi - peak, 1e-12), 1.0)
    w = np.where(inside, np.where(ctr <= peak, up, dn), 0.0)
    s = w.sum()
    if s <= 0:
        w = np.zeros_like(ctr)
        w[int(np.argmin(np.abs(ctr - peak)))] = 1.0
        return w
    return w / s


def compute(bars: pd.DataFrame, turnover: pd.Series, keep: int = 500, nbins: int = NBINS) -> dict:
    """bars：index=日期，列 open/high/low/close。turnover：指数换手率（小数，按日期）。
    返回最近 keep 天每天收盘后的筹码分布（整数，总和 10000）及统计。"""
    n = len(bars)
    H, L, C = (bars[k].to_numpy(float) for k in ("high", "low", "close"))
    tr = turnover.reindex(bars.index).ffill().bfill().to_numpy(float) if len(turnover) else np.full(n, 0.01)
    tr = np.clip(np.where(np.isfinite(tr), tr, 0.01), TR_MIN, TR_MAX)
    ref = slice(max(0, n - GRID_DAYS), n)
    lo, hi = float(L[ref].min()) * 0.97, float(H[ref].max()) * 1.03
    edges = np.linspace(lo, hi, nbins + 1)
    ctr = (edges[:-1] + edges[1:]) / 2
    dist = np.zeros(nbins)
    dist[int(np.argmin(np.abs(ctr - C[0])))] = 1.0
    keep = min(keep, n)
    snaps = np.zeros((keep, nbins))
    stats = {k: np.zeros(keep) for k in ("profit", "avg", "lo90", "hi90", "lo70", "hi70")}
    for i in range(n):
        l, h, c = min(max(L[i], lo), hi), min(max(H[i], lo), hi), min(max(C[i], lo), hi)
        peak = min(max((h + l + 2 * c) / 4, l), h)
        dist = dist * (1 - tr[i]) + tr[i] * _triangle(ctr, l, h, peak)
        j = i - (n - keep)
        if j >= 0:
            snaps[j] = dist
            cum = np.concatenate([[0.0], np.cumsum(dist)])
            cum /= cum[-1]
            stats["profit"][j] = float(np.interp(c, edges, cum))
            stats["avg"][j] = float((dist * ctr).sum() / dist.sum())
            for q, a, b in ((90, 0.05, 0.95), (70, 0.15, 0.85)):
                stats[f"lo{q}"][j] = float(np.interp(a, cum, edges))
                stats[f"hi{q}"][j] = float(np.interp(b, cum, edges))
    return {"bins": [round(float(x), 2) for x in ctr], "step": round(float(edges[1] - edges[0]), 4),
            "dates": list(bars.index[-keep:]),
            "dist": np.round(snaps * 10000).astype(int).tolist(),
            "stats": {k: [round(float(x), 4 if k == "profit" else 2) for x in v] for k, v in stats.items()}}
