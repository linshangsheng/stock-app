"""宽表加载：把 daily_bar 读成「日期 × 股票」宽表（pandas 向量化，不逐只循环；5.4 性能目标）。
价格口径（3.6.1）：库内存不复权原始价 + adj_factor；特征用前复权序列（以序列最后一日为基准现算）；
涨跌停 / 最小价位 / 股数取整等依赖真实价格的判定用不复权价。
停牌日（trade_status=0）的价量置 NaN，不做填充。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Panel:
    dates: pd.Index
    symbols: pd.Index
    raw: dict[str, pd.DataFrame]           # open/high/low/close 不复权
    adj: dict[str, pd.DataFrame]           # open/high/low/close 前复权
    volume: pd.DataFrame
    amount: pd.DataFrame
    turnover: pd.DataFrame
    status: pd.DataFrame                   # 1 正常 0 停牌（有行情行但停牌）
    is_st: pd.DataFrame
    has_row: pd.DataFrame                  # 当日是否有数据行（用于覆盖率）
    is_temp: pd.DataFrame
    meta: dict = field(default_factory=dict)

    @property
    def close(self) -> pd.DataFrame:
        return self.adj["close"]

    def slice_dates(self, start: str | None = None, end: str | None = None) -> "Panel":
        m = pd.Series(True, index=self.dates)
        if start:
            m &= self.dates >= start
        if end:
            m &= self.dates <= end
        idx = self.dates[m.values]
        sel = lambda df: df.loc[idx]  # noqa: E731
        return Panel(idx, self.symbols, {k: sel(v) for k, v in self.raw.items()},
                     {k: sel(v) for k, v in self.adj.items()}, sel(self.volume), sel(self.amount),
                     sel(self.turnover), sel(self.status), sel(self.is_st), sel(self.has_row), sel(self.is_temp),
                     dict(self.meta))


def _bar_query(start: str | None, end: str | None, symbols: list[str] | None, only_l1: bool) -> tuple[str, list]:
    q = "SELECT symbol,date,open,high,low,close,volume,amount,turnover,adj_factor,trade_status,is_st,is_temp FROM daily_bar"
    conds, args = [], []
    if start:
        conds.append("date>=?")
        args.append(start)
    if end:
        conds.append("date<=?")
        args.append(end)
    if only_l1:
        conds.append("symbol IN (SELECT symbol FROM securities WHERE in_l1=1 OR status='delisted' OR "
                     "symbol IN (SELECT DISTINCT symbol FROM scan_results))")
    if symbols:
        conds.append("symbol IN (%s)" % ",".join("?" * len(symbols)))
        args += symbols
    if conds:
        q += " WHERE " + " AND ".join(conds)
    return q, args


def load_frame(conn, start: str | None = None, end: str | None = None, symbols: list[str] | None = None,
               only_l1: bool = True) -> pd.DataFrame:
    """daily_bar 的长表（分块加载等场景用；整个面板请用 load_panel，它不经过这张长表）。"""
    q, args = _bar_query(start, end, symbols, only_l1)
    return pd.read_sql_query(q, conn, params=args)


NUM_COLS = ("open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor", "trade_status", "is_st", "is_temp")
CHUNK = 100_000


def load_columns(conn, start: str | None = None, end: str | None = None, symbols: list[str] | None = None,
                 only_l1: bool = True):
    """从数据库一块一块地读，直接填进数值数组，**不先建那张巨大的长表**。
    pd.read_sql_query 会先把每一行变成 Python 对象和字符串再转成表：长表本身 47 MB 时，读的过程峰值要 179 MB（近 4 倍），
    全市场（约 3900 只 × 430 日）会短暂吃掉 1 GB 以上，用完还不还给系统。这里的峰值约等于最终那几个数组的大小。
    返回 (日期索引, 股票索引, 行的日期位置, 行的股票位置, {列名: 一维 float64 数组})；排序、缺失值处理与 pd.to_numeric(errors='coerce') 一致。"""
    q, args = _bar_query(start, end, symbols, only_l1)
    cur = conn.cursor()
    cur.row_factory = None                                         # 要普通元组，不要 sqlite3.Row（更快、更省）
    cur.execute(q, args)
    sym_id: dict[str, int] = {}
    date_id: dict[str, int] = {}
    sy_c, dd_c, num_c = [], [], []
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        n = len(rows)
        sy_c.append(np.fromiter((sym_id.setdefault(r[0], len(sym_id)) for r in rows), dtype=np.int32, count=n))
        dd_c.append(np.fromiter((date_id.setdefault(r[1], len(date_id)) for r in rows), dtype=np.int32, count=n))
        num_c.append(np.array([r[2:] for r in rows], dtype=np.float64))      # None -> NaN
        del rows
    cur.close()
    total = sum(len(x) for x in sy_c)
    s_sorted, d_sorted = sorted(sym_id), sorted(date_id)
    s_rank = np.empty(len(sym_id), dtype=np.int32)
    d_rank = np.empty(len(date_id), dtype=np.int32)
    for new, key in enumerate(s_sorted):
        s_rank[sym_id[key]] = new
    for new, key in enumerate(d_sorted):
        d_rank[date_id[key]] = new
    si = np.empty(total, dtype=np.int32)
    di = np.empty(total, dtype=np.int32)
    cols = {c: np.empty(total, dtype=np.float64) for c in NUM_COLS}
    pos = 0
    while sy_c:                                                    # 边拷贝边释放分块，峰值不会翻倍
        sy, dd, nm = sy_c.pop(0), dd_c.pop(0), num_c.pop(0)
        n = len(sy)
        si[pos:pos + n], di[pos:pos + n] = s_rank[sy], d_rank[dd]
        for j, c in enumerate(NUM_COLS):
            cols[c][pos:pos + n] = nm[:, j]
        pos += n
    return pd.Index(d_sorted, name="date"), pd.Index(s_sorted, name="symbol"), di, si, cols


def load_panel(conn, start: str | None = None, end: str | None = None, symbols: list[str] | None = None,
               only_l1: bool = True, float32: bool = True, market: str = "CN") -> Panel:
    d_idx, s_idx, di, si, cols = load_columns(conn, start, end, symbols, only_l1)
    splits = None
    if len(di) and market == "US":
        splits = pd.read_sql_query("SELECT symbol, ex_date, ratio_or_amount AS ratio FROM corp_actions WHERE type='split'", conn)
    return _build_panel(d_idx, s_idx, di, si, cols, float32, splits)


def split_restore_ratio(dates: pd.Index, symbols: pd.Index, splits: pd.DataFrame | None) -> pd.DataFrame | None:
    """美股「当时真实价」还原系数（3.6.1）：yfinance 的 OHLC / 成交量已按拆股调整，真实价 = 调整价 × 该日之后所有拆股的累计比例。
    比例 r 表示 r 拆 1（正向拆股 r>1，反向拆股 r<1）。"""
    if splits is None or splits.empty:
        return None
    a = np.ones((len(dates), len(symbols)), dtype=np.float64)
    sidx = {s: j for j, s in enumerate(symbols)}
    d = np.asarray(dates, dtype=str)
    for r in splits.itertuples():
        j = sidx.get(r.symbol)
        if j is None or not (r.ratio and r.ratio > 0):
            continue
        k = int(np.searchsorted(d, str(r.ex_date)[:10]))      # 除权日之前（不含）的日期需要还原
        a[:k, j] *= float(r.ratio)
    return pd.DataFrame(a, index=dates, columns=symbols)


def panel_from_frame(df: pd.DataFrame, float32: bool = True, splits: pd.DataFrame | None = None) -> Panel:
    if df.empty:
        return _build_panel(None, None, None, None, None, float32, splits)
    d_idx = pd.Index(np.sort(df["date"].unique()), name="date")
    s_idx = pd.Index(np.sort(df["symbol"].unique()), name="symbol")
    di, si = d_idx.get_indexer(df["date"]), s_idx.get_indexer(df["symbol"])
    cols = {c: pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=np.float64) for c in NUM_COLS}
    return _build_panel(d_idx, s_idx, di, si, cols, float32, splits)


def _build_panel(d_idx, s_idx, di, si, cols, float32: bool, splits) -> Panel:
    """把「行 -> (日期位置, 股票位置, 各列数值)」铺成日期 × 股票的宽表，并算前复权。load_panel 与 panel_from_frame 共用这一份。
    cols 里的数组用一个就丢一个（pop），不让十几张 float64 宽表同时留在内存里。"""
    dt = np.float32 if float32 else np.float64
    if d_idx is None or len(d_idx) == 0:
        e = pd.DataFrame()
        return Panel(pd.Index([]), pd.Index([]), {k: e for k in ("open", "high", "low", "close")},
                     {k: e for k in ("open", "high", "low", "close")}, e, e, e, e, e, e, e)

    # 比逐列 df.pivot 快数倍：一次建好行列索引，再按位置赋值（5000 只 × 430 日：约 20s -> 数秒）
    def wide(col):
        a = np.full((len(d_idx), len(s_idx)), np.nan, dtype=np.float64)
        a[di, si] = cols.pop(col)
        return pd.DataFrame(a, index=d_idx, columns=s_idx)

    raw = {c: wide(c) for c in ("open", "high", "low", "close")}
    dates, symbols = raw["close"].index, raw["close"].columns
    factor = wide("adj_factor").reindex(index=dates, columns=symbols).ffill()
    # 前复权：以每只股票序列最后一日因子为基准。
    last_fac = factor.ffill().iloc[-1]
    ratio = factor.div(last_fac, axis=1)
    del factor
    status = wide("trade_status").reindex(index=dates, columns=symbols)
    has_row = raw["close"].notna() | status.notna()
    status = status.fillna(0)
    trading = status > 0
    adj = {}
    for k, v in raw.items():
        adj[k] = (v * ratio).where(trading).astype(dt)
    del ratio
    volume = wide("volume").where(trading).astype(dt)
    amount = wide("amount").where(trading).astype(dt)
    turnover = wide("turnover").where(trading).astype(dt)
    is_st = wide("is_st").reindex(index=dates, columns=symbols).fillna(0)
    is_temp = wide("is_temp").reindex(index=dates, columns=symbols).fillna(0)
    real = split_restore_ratio(dates, symbols, splits)
    if real is not None:                       # 美股：raw = 当时真实价；adj = 拆股调整价（特征计算用）
        raw = {k: v * real for k, v in raw.items()}
    raw = {k: v.astype(dt) for k, v in raw.items()}
    return Panel(dates, symbols, raw, adj, volume, amount, turnover, status.astype(np.int8),
                 is_st.astype(np.int8), has_row, is_temp.astype(np.int8), {})
