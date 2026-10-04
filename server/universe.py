"""股票池分层（3.26.1）：入库池 L1（宽松，决定「存哪些股票」）与交易池 L2（严格，逐日点时，决定「扫描 / 回测能买哪些」）。
L2 只使用日线可直接算出的点时条件（价格、成交额、上市天数、停牌 / 一字板天数、当日 ST），**不使用市值**（免费源无历史市值序列）。
只增不删：L1 剔除只标记 in_l1=0，历史日线永不删除；已退市股票一律保留。"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import markets, settings
from .panel import Panel


# ---- 涨跌幅规则（带生效日期，5.5.1 / 6.4）-------------------------------------

def _rule_vector(rules: list[dict], dates: pd.Index) -> np.ndarray:
    out = np.full(len(dates), np.nan)
    d = np.asarray(dates, dtype=str)
    for r in sorted(rules, key=lambda r: str(r["from"])):
        out[d >= str(r["from"])] = r["pct"]
    return out


def limit_pct_matrix(dates: pd.Index, symbols: pd.Index, boards: pd.Series, is_st: pd.DataFrame,
                     rules: dict | None = None) -> pd.DataFrame:
    """每个 (日期, 股票) 的涨跌幅限制比例。未知板块按主板处理。"""
    rules = rules or settings.cfg()["limit_rules"]
    vec = {k: _rule_vector(v, dates) for k, v in rules.items()}
    base = np.empty((len(dates), len(symbols)))
    st_alt = np.empty_like(base)
    for j, s in enumerate(symbols):
        b = boards.get(s, "main") or "main"
        base[:, j] = vec.get(b, vec["main"])
        st_alt[:, j] = vec["st_main"] if b == "main" else vec.get("st_other", vec.get(b, vec["main"]))
    st = is_st.reindex(index=dates, columns=symbols).fillna(0).to_numpy() > 0
    m = np.where(st, st_alt, base)
    return pd.DataFrame(np.nan_to_num(m, nan=0.10), index=dates, columns=symbols)


def limit_flags(panel: Panel, boards: pd.Series, tol: float = 0.0015, market: str = "CN") -> dict[str, pd.DataFrame]:
    """涨停 / 跌停 / 一字板（用不复权价，3.6.1）。涨跌停价按 round(前收 × (1±pct), 2) 判定。美股无涨跌停：全部为 False。"""
    if not markets.has_price_limits(market):
        z = pd.DataFrame(False, index=panel.dates, columns=panel.symbols)
        zf = pd.DataFrame(0.0, index=panel.dates, columns=panel.symbols)
        return {"limit_up": z, "limit_down": z, "open_limit_up": z, "open_limit_down": z, "oneword": z,
                "limit_pct": zf, "up_px": zf, "dn_px": zf}
    lim = limit_pct_matrix(panel.dates, panel.symbols, boards, panel.is_st)
    c = panel.raw["close"].astype(float)
    pc = c.shift(1)
    up_px = (pc * (1 + lim)).round(2)
    dn_px = (pc * (1 - lim)).round(2)
    hi, lo = panel.raw["high"].astype(float), panel.raw["low"].astype(float)
    op = panel.raw["open"].astype(float)
    trading = panel.status > 0
    eps = 0.005 + tol * 0  # 价格最小变动 0.01 元的半档
    limit_up = trading & (c >= up_px - eps)
    limit_dn = trading & (c <= dn_px + eps)
    open_up = trading & (op >= up_px - eps)
    open_dn = trading & (op <= dn_px + eps)
    oneword = trading & (hi == lo) & (limit_up | limit_dn) & (panel.volume.fillna(0) > 0)
    return {"limit_up": limit_up, "limit_down": limit_dn, "open_limit_up": open_up, "open_limit_down": open_dn,
            "oneword": oneword, "limit_pct": lim, "up_px": up_px, "dn_px": dn_px}


# ---- L2 ---------------------------------------------------------------------

def list_age_days(panel: Panel, list_dates: pd.Series) -> pd.DataFrame:
    """上市交易日数。已知上市日期早于库内首条记录者视为足够老；未知上市日期（美股）时，
    窗口开始时就已有数据的视为足够老，窗口内才出现的（新上市）按累计交易日数计。"""
    age = panel.has_row.astype(int).cumsum()
    first_bar = pd.to_datetime(panel.has_row.idxmax().reindex(panel.symbols))
    ldt = pd.to_datetime(list_dates.reindex(panel.symbols), errors="coerce")
    known_old = ldt < first_bar - pd.Timedelta(days=10)
    window_start = pd.to_datetime(panel.dates[0]) if len(panel.dates) else pd.Timestamp.min
    unknown_old = ldt.isna() & (first_bar <= window_start + pd.Timedelta(days=10))
    old_s = (known_old | unknown_old).fillna(False).to_numpy(dtype=bool)
    age = age.copy()
    age.loc[:, old_s] = 10_000
    return age


def l2_mask(panel: Panel, boards: pd.Series, list_dates: pd.Series, params: dict | None = None,
            return_parts: bool = False, market: str = "CN"):
    p = params or markets.universe_cfg(market)["l2"]
    flags = limit_flags(panel, boards, market=market)
    age = list_age_days(panel, list_dates)
    cond = {}
    cond["tradable"] = (panel.status > 0) & panel.has_row
    cond["list_age"] = age >= p["min_list_days"]
    cond["price"] = panel.raw["close"].astype(float) >= p["min_price"]
    adv20 = panel.amount.astype(float).rolling(20, min_periods=12).mean()
    cond["liquidity"] = adv20 >= p["min_avg_amount20"]
    started = panel.has_row.cummax()                       # 已有首条记录之后：缺失行 = 暂停交易（美股停牌不出行）
    susp60 = ((panel.status == 0) & (panel.has_row | started)).astype(int).rolling(60, min_periods=1).sum()
    cond["suspend"] = susp60 <= p["max_suspend_60d"]
    ow20 = flags["oneword"].astype(int).rolling(20, min_periods=1).sum()
    cond["oneword"] = ow20 <= p["max_oneword_20d"]
    cond["not_st"] = (panel.is_st == 0) if p.get("exclude_st", True) else pd.DataFrame(True, index=panel.dates, columns=panel.symbols)
    mask = pd.DataFrame(True, index=panel.dates, columns=panel.symbols)
    for v in cond.values():
        mask &= v.fillna(False)
    if return_parts:
        return mask, cond, flags
    return mask


def l2_exclusion_stats(cond: dict[str, pd.DataFrame], day_index: int = -1) -> dict:
    """最近一日各条件的剔除数量（设置页展示，3.26.1-4）。逐项独立统计（一只股票可同时被多项剔除）。"""
    out = {}
    for k, v in cond.items():
        row = v.iloc[day_index]
        out[k] = int((~row.fillna(False)).sum())
    return out


# ---- L1 ---------------------------------------------------------------------

def apply_l1(conn, snapshot: pd.DataFrame | None = None, asof: str | None = None, market: str = "CN") -> dict:
    """应用 L1 入库过滤，返回各条件剔除数量（3.26.1）。
    有全市场快照时按 价格 / 流通市值 / 成交额 做入库粗筛；无快照时仅按板块与证券类型（L2 逐日兜底）。
    已退市股票一律入库；ST 不在 L1 剔除；只改 in_l1 标记，不删数据。"""
    p = markets.universe_cfg(market)
    l1 = p["l1"]
    sec = pd.read_sql_query("SELECT symbol,board,status,list_date FROM securities", conn)
    asof = asof or date.today().isoformat()
    stats = {"total": len(sec), "board": 0, "price": 0, "mktcap": 0, "amount": 0, "kept_delisted": 0}
    keep = pd.Series(True, index=sec.index)
    if "boards" in p:                                     # 板块过滤仅 A 股（美股名单阶段已剔除非普通股）
        keep &= sec["board"].isin(p["boards"]) | sec["board"].isna()
        stats["board"] = int((~sec["board"].isin(p["boards"])).sum())
    delisted = sec["status"] == "delisted"
    stats["kept_delisted"] = int(delisted.sum())
    if snapshot is not None and len(snapshot):
        snap = snapshot.set_index("symbol")
        px = sec["symbol"].map(snap["close"])
        cap = sec["symbol"].map(snap["float_mktcap"])
        amt = sec["symbol"].map(snap["amount"])
        bad_px = px.notna() & (px < l1["min_price"])
        bad_cap = cap.notna() & (cap < l1["min_float_mktcap"])
        bad_amt = amt.notna() & (amt < l1["min_avg_amount20"] * 0.5)   # 单日成交额口径，阈值再放宽一半
        stats.update(price=int((bad_px & ~delisted).sum()), mktcap=int((bad_cap & ~delisted).sum()),
                     amount=int((bad_amt & ~delisted).sum()))
        keep &= ~(bad_px | bad_cap | bad_amt)
    keep |= delisted
    if snapshot is None:                                  # 无快照：保留此前预筛（prefilter_l1）已剔除的标记，不要把垃圾股翻回来
        pre = {r[0] for r in conn.execute("SELECT symbol FROM fetch_state WHERE task='prefilter' AND status='excluded'")}
        keep &= ~(sec["symbol"].isin(pre) & ~delisted)
    conn.execute("UPDATE securities SET in_l1=0")
    conn.executemany("UPDATE securities SET in_l1=1, l1_asof=? WHERE symbol=?",
                     [(asof, s) for s in sec.loc[keep, "symbol"]])
    stats["l1_size"] = int(keep.sum())
    from . import db as _db
    import json as _json
    _db.set_meta(conn, "l1_stats", _json.dumps({**stats, "asof": asof}, ensure_ascii=False))
    return stats


def load_security_meta(conn) -> tuple[pd.Series, pd.Series, pd.Series]:
    sec = pd.read_sql_query("SELECT symbol,board,list_date,name FROM securities", conn).set_index("symbol")
    return sec["board"], sec["list_date"], sec["name"]
