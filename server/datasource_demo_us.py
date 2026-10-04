"""美股演示数据源（合成）：用于离线演示与测试，**不代表真实行情**。
复用 A 股演示数据的生成器，但按美股口径输出：无涨跌停 / ST；价格与成交量为「拆股调整」口径（与 yfinance 一致），
拆股事件另存（真实价 = 调整价 × 之后所有拆股的累计比例）；指数为 SPY / QQQ / IWM / DIA / ^GSPC / ^IXIC / ^VIX。"""
from __future__ import annotations

import zlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .datasource_demo import DemoSource
from .datasource_us import US_BAR_COLS

US_HOLIDAYS = [(1, 1), (1, 20), (2, 17), (5, 26), (6, 19), (7, 4), (9, 1), (11, 27), (12, 25)]
SECTORS = ["Technology", "Healthcare", "Financial Services", "Consumer Cyclical", "Industrials", "Energy", "Communication Services",
           "Consumer Defensive", "Utilities", "Basic Materials", "Real Estate", "Semiconductors"]
US_INDEX = {**{e: (0.8 + 0.05 * i, 90 + 10 * i) for i, e in enumerate(["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLU", "XLB", "XLRE", "XLC"])}, "SPY": (1.0, 450), "QQQ": (1.25, 380), "IWM": (1.15, 190), "DIA": (0.85, 350), "^GSPC": (1.0, 4500), "^IXIC": (1.3, 14000)}


class DemoUsSource(DemoSource):
    name = "demo-us"

    def __init__(self, n_stocks: int = 200, **kw):
        kw.setdefault("seed", 11)
        super().__init__(n_stocks=n_stocks, **kw)
        self._cu: dict = {}

    def _holiday(self, d: date) -> bool:
        return (d.month, d.day) in US_HOLIDAYS

    def _make_securities(self) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed + 7)
        n = len(self.dates)
        rows = []
        for i in range(self.n_stocks):
            sym = "".join(chr(65 + (i // 26 ** k) % 26) for k in (2, 1, 0))
            list_idx = 0 if rng.random() > 0.12 else int(rng.integers(n // 3, n - 300))
            delist_idx = None
            if i >= self.n_stocks - 10:
                delist_idx = int(rng.integers(n // 4, n - 60))
                list_idx = min(list_idx, max(delist_idx - 400, 0))
            rows.append({"symbol": sym, "name": f"Demo Corp {sym}", "board": "main", "industry": SECTORS[i % len(SECTORS)],
                         "list_idx": list_idx, "delist_idx": delist_idx, "st_prone": False})
        return pd.DataFrame(rows)

    def trade_calendar(self, start: str, end: str):
        rows = super().trade_calendar(start, end)
        half = {(11, 28), (12, 24)}
        out = []
        for d, o, _ in rows:
            dd = date.fromisoformat(d)
            out.append((d, o, 1 if o and (dd.month, dd.day) in half else 0))
        return out

    def list_current_securities(self) -> pd.DataFrame:
        s = self._securities[self._securities["delist_idx"].isna()][["symbol", "name"]].copy()
        s["exchange"], s["board"] = "NASDAQ", "us"
        return s.reset_index(drop=True)

    # ---- 日线：拆股调整口径 ----
    def _us_bars(self, symbol: str) -> tuple[pd.DataFrame, list[tuple[str, float]]]:
        if symbol in self._cu:
            return self._cu[symbol]
        raw = self._gen(symbol)
        ev = raw.attrs.get("events", [])
        # 父类的复权因子阶跃：约 2x 的视为拆股（2 拆 1），其余（1~3% 的小阶跃）视为分红，不影响拆股调整价
        splits, prev = [], 1.0
        for d, f in ev:
            if f / prev > 1.8:
                splits.append((d, 2.0))
            prev = f
        adj = raw["close"] * raw["adj_factor"]                       # 连续的总回报价，作为「拆股调整后的收盘价」
        k = adj / raw["close"]
        later = np.ones(len(raw))
        dates = raw["date"].to_numpy()
        for d, r in splits:
            later[dates < d] *= r
        df = pd.DataFrame({
            "date": raw["date"], "open": raw["open"] * k, "high": raw["high"] * k, "low": raw["low"] * k, "close": adj,
            "volume": (raw["volume"] * later).round(0), "turnover": np.nan, "adj_factor": 1.0,
            "trade_status": 1, "is_st": 0, "adj_close": adj,
        })
        df["amount"] = df["close"] * df["volume"]
        df["dividend"] = 0.0
        df["split"] = 0.0
        for d, r in splits:
            df.loc[df["date"] == d, "split"] = r
        df = df[df["volume"] > 0].reset_index(drop=True)             # 美股停牌日不出行
        out = df[US_BAR_COLS].round({"open": 4, "high": 4, "low": 4, "close": 4, "adj_close": 4})
        self._cu[symbol] = (out, splits)
        return out, splits

    def daily_bars_batch(self, symbols, start, end):
        known = set(self._securities["symbol"])
        out = {}
        for s in symbols:
            if s in known:
                df, _ = self._us_bars(s)
                out[s] = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
        return out

    def daily_bars(self, symbol, start, end, prev_close=None, prev_factor=None):
        return self.daily_bars_batch([symbol], start, end).get(symbol, pd.DataFrame(columns=US_BAR_COLS))

    def index_bars(self, symbol, start, end):
        if symbol == "^VIX":
            rng = np.random.default_rng(zlib.crc32(b"vix"))
            vol = pd.Series(np.abs(self.mkt)).rolling(10, min_periods=1).mean().to_numpy()
            lvl = (12 + vol * 1200 + rng.normal(0, 0.6, len(vol))).clip(9, 80)
            df = pd.DataFrame({"date": [d.isoformat() for d in self.dates], "open": lvl, "high": lvl * 1.03, "low": lvl * 0.97,
                               "close": lvl, "volume": 0.0, "amount": 0.0})
        elif symbol in US_INDEX:
            beta, lvl0 = US_INDEX[symbol]
            rng = np.random.default_rng(zlib.crc32(symbol.encode()))
            r = beta * self.mkt + rng.normal(0, 0.002, len(self.mkt))
            c = lvl0 * np.exp(np.cumsum(r))
            df = pd.DataFrame({"date": [d.isoformat() for d in self.dates], "open": c, "high": c * 1.004, "low": c * 0.996,
                               "close": c, "volume": 5e7, "amount": c * 5e7})
        else:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        return df[(df["date"] >= start) & (df["date"] <= end)].round(4).reset_index(drop=True)

    def security_basic(self, symbol):
        return {"status": "active", "sec_type": "stock"}

    def industry(self, symbol):
        r = self._securities[self._securities.symbol == symbol]
        return (r["industry"].iloc[0], r["industry"].iloc[0]) if len(r) else (None, None)

    def industry_map(self):
        return self._securities[["symbol", "industry"]].copy()

    def earnings_dates(self, symbol):
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        off = int(rng.integers(5, 25))
        q = date(self.end.year, ((self.end.month - 1) // 3) * 3 + 1, 1) + timedelta(days=off)
        out = []
        for i in range(8):
            dd = q - timedelta(days=91 * i)
            out.append((dd.isoformat(), dd <= self.end))
        out.append(((q + timedelta(days=91)).isoformat(), False))
        return out

    def news(self, symbol, limit=15):
        return [{"title": f"{symbol} demo headline {i}", "summary": "合成演示新闻", "url": None,
                 "publish_time": (self.end - timedelta(days=i)).isoformat(), "source": "Demo Wire"} for i in range(3)]

    def insider_transactions(self, symbol):
        return [{"date": (self.end - timedelta(days=9)).isoformat(), "insider": "Demo Insider", "position": "CEO",
                 "text": "Sale", "shares": 1000, "value": 100000}]
