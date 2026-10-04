"""演示数据源（合成）：仅用于离线演示、界面与流程验证，**不代表任何真实行情，其回测结果没有任何投资参考价值**。
接口与 BaoStockSource 一致；数据写入独立目录（配置 datasource.cn=demo 时建议同时设置 STOCK_DATA_DIR=data/demo）。
生成方式：市场状态（牛 / 熊 / 震荡）马尔可夫切换 + 行业因子 + 个股慢变趋势 + 量价相关 + 停牌 / ST / 除权 / 退市 / 次新。"""
from __future__ import annotations

import zlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import settings
from .datasource_cn import BAR_COLS, board_of

INDUSTRIES = ["银行", "非银金融", "医药生物", "电子", "计算机", "电力设备", "食品饮料", "汽车", "机械设备", "化工", "有色金属", "房地产"]
HOLIDAYS = [(1, 1), (1, 2), (5, 1), (5, 2), (5, 3), (10, 1), (10, 2), (10, 3), (10, 4), (10, 5)]
BENCHMARKS = {"sh.000300": (1.0, 3500), "sh.000001": (0.9, 3000), "sz.399001": (1.1, 11000),
              "sz.399006": (1.3, 2200), "sh.000905": (1.15, 5500), "sh.000852": (1.25, 6000)}


def _delist_idx(row) -> int | None:
    v = row["delist_idx"]
    return None if v is None or v != v else int(v)


def _is_holiday(d: date) -> bool:
    return (d.month, d.day) in HOLIDAYS


class DemoSource:
    name = "demo"

    def __init__(self, n_stocks: int = 160, years: int | None = None, seed: int = 11, end: date | None = None,
                 trend_sd: float = 0.0003, neutral: bool = False):
        """trend_sd：个股慢变趋势强度（0 = 纯随机游走）；neutral：市场 / 行业无漂移（用于验证回测引擎无前视的对照实验）。"""
        self.n_stocks = n_stocks
        self.trend_sd = trend_sd
        self.neutral = neutral
        self.seed = seed
        years = years or min(int(settings.cfg().get("history_years", 10)), 6)
        end = end or date.today()
        while end.weekday() >= 5 or _is_holiday(end):
            end -= timedelta(days=1)
        self.end = end
        self.start = date(end.year - years, end.month, min(end.day, 28))
        self.dates = self._make_dates()
        self._build_factors()
        self._securities = self._make_securities()
        self._cache: dict[str, pd.DataFrame] = {}

    # ---- 日历 ----
    def _make_dates(self) -> list[date]:
        out, d = [], self.start
        while d <= self.end:
            if d.weekday() < 5 and not _is_holiday(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def trade_calendar(self, start: str, end: str) -> list[tuple[str, int, int]]:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        have = {d for d in self.dates}
        out, d = [], s
        while d <= e:
            out.append((d.isoformat(), 1 if d in have else 0, 0))
            d += timedelta(days=1)
        return out

    # ---- 因子 ----
    def _build_factors(self):
        rng = np.random.default_rng(self.seed)
        n = len(self.dates)
        drift = {0: 0.0009, 1: -0.0011, 2: 0.0001} if not self.neutral else {0: 0.0, 1: 0.0, 2: 0.0}
        vol = {0: 0.0085, 1: 0.016, 2: 0.011}
        trans = np.array([[0.992, 0.003, 0.005], [0.015, 0.975, 0.010], [0.010, 0.006, 0.984]])
        st, states = 0, []
        for _ in range(n):
            st = rng.choice(3, p=trans[st])
            states.append(st)
        self.regime_states = np.array(states)
        self.mkt = np.array([rng.normal(drift[s], vol[s]) for s in states])
        self.ind = np.zeros((len(INDUSTRIES), n))
        for k in range(len(INDUSTRIES)):
            slow = 0.0
            for t in range(n):
                slow = 0.985 * slow + rng.normal(0, 0.0004 if not self.neutral else 0.0)
                self.ind[k, t] = slow + rng.normal(0, 0.004)

    # ---- 证券主表 ----
    def _make_securities(self) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed + 1)
        rows = []
        prefixes = [("sh.60", 6, "main"), ("sz.00", 6, "main"), ("sz.30", 6, "chinext"), ("sh.688", 6, "star")]
        n = len(self.dates)
        for i in range(self.n_stocks):
            ex, _, board = prefixes[i % 4]
            code = f"{ex}{(i // 4) + 1:0{6 - len(ex.split('.')[1])}d}"
            list_idx = 0 if rng.random() > 0.15 else int(rng.integers(n // 3, n - 300))
            delist_idx = None
            if i >= self.n_stocks - 12:                 # 窗口期内已退市
                delist_idx = int(rng.integers(n // 4, n - 60))
                list_idx = min(list_idx, delist_idx - 400) if delist_idx > 450 else 0
            rows.append({"symbol": code, "name": f"演示股票{i + 1:03d}", "board": board,
                         "industry": INDUSTRIES[i % len(INDUSTRIES)], "list_idx": max(list_idx, 0),
                         "delist_idx": delist_idx, "st_prone": bool(i % 23 == 5)})
        return pd.DataFrame(rows)

    def list_securities_union(self, days: list[str]) -> pd.DataFrame:
        return self._securities[["symbol", "name", "board"]].copy()

    def security_basic(self, symbol: str) -> dict:
        r = self._securities[self._securities.symbol == symbol]
        if r.empty:
            return {}
        r = r.iloc[0]
        di = _delist_idx(r)
        return {"name": r["name"], "list_date": self.dates[int(r["list_idx"])].isoformat(),
                "delist_date": self.dates[di].isoformat() if di is not None else None,
                "status": "delisted" if di is not None else "active", "sec_type": "stock"}

    def report_pub_date(self, symbol: str, year: int, quarter: int) -> str | None:
        """合成的财报披露日：季末后 25~40 天（逐股固定），晚于数据末日则视为未披露。"""
        qend = date(year, quarter * 3, [31, 30, 30, 31][quarter - 1])
        jitter = 25 + (zlib.crc32(f"{symbol}{quarter}".encode()) % 16)
        d = qend + timedelta(days=jitter)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat() if d <= self.end else None

    def industry_map(self) -> pd.DataFrame:
        return self._securities[["symbol", "industry"]].copy()

    # ---- 单只生成 ----
    def _gen(self, symbol: str) -> pd.DataFrame:
        if symbol in self._cache:
            return self._cache[symbol]
        sec = self._securities[self._securities.symbol == symbol].iloc[0]
        rng = np.random.default_rng(zlib.crc32(symbol.encode()) ^ self.seed)
        n = len(self.dates)
        i0 = int(sec["list_idx"])
        di = _delist_idx(sec)
        i1 = di if di is not None else n - 1
        ind_k = INDUSTRIES.index(sec["industry"]) if sec["industry"] in INDUSTRIES else zlib.crc32(sec["industry"].encode()) % len(INDUSTRIES)
        beta = rng.uniform(0.7, 1.4)
        sigma = rng.uniform(0.012, 0.026)
        lim = {"main": 0.10, "chinext": 0.20, "star": 0.20}[sec["board"]]
        m = i1 - i0 + 1
        slow, idio_prev = 0.0, 0.0
        ret = np.zeros(m)
        for t in range(m):
            slow = 0.97 * slow + rng.normal(0, self.trend_sd)
            idio = 0.05 * idio_prev + rng.normal(0, sigma)
            idio_prev = idio
            ret[t] = beta * self.mkt[i0 + t] + 0.6 * self.ind[ind_k, i0 + t] + slow + idio
        if di is not None:
            ret[-120:] -= 0.012                            # 退市前持续下跌
        ret = np.clip(ret, -lim * 0.98, lim * 0.98)
        adj_close = np.exp(np.log(rng.uniform(6, 60)) + np.cumsum(ret))
        # 除权：raw = adj / factor，factor 在除权日阶跃上升（价格下跌 r）
        n_ev = int(rng.integers(2, 7))
        ev_idx = sorted(rng.choice(np.arange(10, m - 5), size=min(n_ev, max(m - 16, 1)), replace=False)) if m > 30 else []
        factor = np.ones(m)
        events = []
        for e in ev_idx:
            r = rng.choice([0.985, 0.99, 0.97, 0.5], p=[0.4, 0.35, 0.2, 0.05])
            factor[e:] /= r
            events.append((self.dates[i0 + e].isoformat(), float(factor[e])))
        raw_close = adj_close / factor
        prev = np.r_[raw_close[0], raw_close[:-1]]
        gap = rng.normal(0, 0.35 * sigma, m)
        raw_open = prev * (1 + gap)
        raw_open = np.where(np.arange(m) == 0, raw_close * 0.99, raw_open)
        hi = np.maximum(raw_open, raw_close) * (1 + np.abs(rng.normal(0, 0.4 * sigma, m)))
        lo = np.minimum(raw_open, raw_close) * (1 - np.abs(rng.normal(0, 0.4 * sigma, m)))
        base = np.exp(rng.uniform(np.log(2e6), np.log(6e7)))
        absr = np.abs(np.r_[0, np.diff(np.log(raw_close * factor))])
        vol = base * np.exp(rng.normal(0, 0.25, m)) * (1 + 1.8 * absr / sigma)
        float_shares = base * rng.uniform(60, 200)
        status = np.ones(m, dtype=int)
        for _ in range(int(rng.integers(0, 4))):          # 偶发停牌
            s = int(rng.integers(30, max(m - 15, 31)))
            status[s:s + int(rng.integers(1, 9))] = 0
        is_st = np.zeros(m, dtype=int)
        if sec["st_prone"] and m > 400:
            s = int(rng.integers(150, m - 150))
            is_st[s:s + 120] = 1
        amount = vol * (raw_open + hi + lo + raw_close) / 4
        sus = status == 0
        raw_open = np.where(sus, prev, raw_open)
        hi = np.where(sus, prev, hi)
        lo = np.where(sus, prev, lo)
        close_f = raw_close.copy()
        # 停牌日价格持平上一日
        for t in range(1, m):
            if sus[t]:
                close_f[t] = close_f[t - 1]
        vol = np.where(sus, 0.0, vol)
        amount = np.where(sus, 0.0, amount)
        df = pd.DataFrame({
            "date": [d.isoformat() for d in self.dates[i0:i1 + 1]],
            "open": raw_open.round(2), "high": hi.round(2), "low": lo.round(2), "close": close_f.round(2),
            "volume": np.round(vol, 0), "amount": amount.round(0), "turnover": (vol / float_shares * 100).round(4),
            "adj_factor": factor, "trade_status": status, "is_st": is_st,
        })
        # 保证 high/low 与 O/C 一致（四舍五入后）
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        df.attrs["events"] = events
        self._cache[symbol] = df
        return df

    def daily_bars(self, symbol: str, start: str, end: str, prev_close=None, prev_factor=None) -> pd.DataFrame:
        df = self._gen(symbol)
        return df[(df["date"] >= start) & (df["date"] <= end)][BAR_COLS].reset_index(drop=True)

    def adj_factor_events(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        self._gen(symbol)
        ev = self._cache[symbol].attrs.get("events", [])
        return pd.DataFrame(ev, columns=["date", "factor"])

    def index_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        if symbol not in BENCHMARKS:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        beta, lvl = BENCHMARKS[symbol]
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        r = beta * self.mkt + rng.normal(0, 0.002, len(self.mkt))
        close = lvl * np.exp(np.cumsum(r))
        df = pd.DataFrame({"date": [d.isoformat() for d in self.dates], "close": close})
        df["open"] = df["close"].shift(1).fillna(df["close"]) * (1 + rng.normal(0, 0.002, len(df)))
        df["high"] = df[["open", "close"]].max(axis=1) * 1.004
        df["low"] = df[["open", "close"]].min(axis=1) * 0.996
        df["volume"] = 3e9 * np.exp(rng.normal(0, 0.2, len(df)))
        df["amount"] = df["volume"] * 10
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        return df[["date", "open", "high", "low", "close", "volume", "amount"]].round(2).reset_index(drop=True)

    def close(self):
        pass
