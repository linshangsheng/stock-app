"""美股取数（2.3 / 3.3.2）：yfinance（日线 + 拆股 / 分红 + 行业 + 财报日期）+ Nasdaq Trader 证券清单 + exchange_calendars 交易日历。
字段映射层：把上游统一成与 A 股相同的标准结构。

M0 实测结论（2026-10，本机；脚本 tools/m0_probe_us.py）：
  * yfinance 的 Close / Volume **已按拆股调整**（NVDA 2024-06-05 显示 122.44，真实价约 1224；成交量同口径放大）；
    OHLC 不含分红调整，`Adj Close` 含分红调整（仅作校验）。故必须同时保存拆股事件，价格门槛类条件用「还原后的当时价」（3.6.1）；
  * 调整价 × 调整量 = 真实成交额，可直接用于成交额类条件；
  * `yf.download(..., actions=True)` 一次请求同时返回 Dividends / Stock Splits；批量 100 只 10 年日线约 42 秒、无失败（权证等无数据者为空）；
  * 免费源没有已退市股票的历史：美股回测存在幸存者偏差，结果只作上界参考（3.26.1-5）。
yfinance 被限流的表现（YFRateLimitError / 空 DataFrame / JSON 解码错误）一律按限流处理，触发退避，而不是当作「该股无数据」（3.26.3）。"""
from __future__ import annotations

import io
import re
from datetime import date, timedelta

import pandas as pd

from . import settings, throttle
from .datasource_cn import BAR_COLS
from .throttle import RateLimited

US_BAR_COLS = BAR_COLS + ["adj_close", "dividend", "split"]
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
# 非普通股 / 壳公司的名称特征（L1 类型过滤：优先股、权证、单位、权利、存托凭证、SPAC）
_NON_COMMON = re.compile(r"\b(?:warrants?|rights?|units?|preferred|depositary|notes?\b|trust preferred|%|"
                         r"acquisition|blank check|spac)\b", re.I)


def to_yahoo(symbol: str) -> str:
    return symbol.replace(".", "-")


def parse_nasdaq_lists(nasdaq_txt: str, other_txt: str) -> pd.DataFrame:
    """Nasdaq Trader 清单 -> symbol, name, exchange（仅 NYSE / Nasdaq / NYSE American 普通股；剔除 ETF、测试标的、优先股、权证等）。"""
    rows = []
    n = pd.read_csv(io.StringIO(nasdaq_txt), sep="|", dtype=str)
    n = n[n["Symbol"].notna() & ~n["Symbol"].str.startswith("File Creation", na=False)]
    n = n[(n["Test Issue"] == "N") & (n["ETF"] == "N") & (n["Financial Status"].isin(["N"]))]
    rows += [(a, b, "NASDAQ") for a, b in zip(n["Symbol"], n["Security Name"])]
    o = pd.read_csv(io.StringIO(other_txt), sep="|", dtype=str)
    o = o[o["ACT Symbol"].notna() & ~o["ACT Symbol"].str.startswith("File Creation", na=False)]
    o = o[(o["Test Issue"] == "N") & (o["ETF"] == "N") & (o["Exchange"].isin(["N", "A"]))]
    rows += [(a, b, {"N": "NYSE", "A": "NYSE American"}[c]) for a, b, c in zip(o["ACT Symbol"], o["Security Name"], o["Exchange"])]
    df = pd.DataFrame(rows, columns=["symbol", "name", "exchange"])
    bad_sym = df["symbol"].str.contains(r"[$^/ ]", regex=True) | df["symbol"].str.len().gt(5)
    df = df[~bad_sym & ~df["name"].fillna("").str.contains(_NON_COMMON)]
    df["symbol"] = df["symbol"].map(to_yahoo)
    return df.drop_duplicates("symbol").reset_index(drop=True)


class UsSource:
    name = "yfinance"

    def __init__(self):
        self.th = throttle.get("yfinance")
        self._yf = None

    @property
    def yf(self):
        if self._yf is None:
            import yfinance as yf
            self._yf = yf
        return self._yf

    # ---- 日历（exchange_calendars XNYS：节假日 / 夏令时 / 提前收盘日）----
    def trade_calendar(self, start: str, end: str) -> list[tuple[str, int, int]]:
        import exchange_calendars as xc

        cal = xc.get_calendar("XNYS")
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        lo, hi = max(s, cal.first_session), min(e, cal.last_session)
        sessions = cal.sessions_in_range(lo, hi)
        half = {d.date().isoformat() for d in cal.early_closes if lo <= d <= hi}
        have = {d.date().isoformat() for d in sessions}
        out, d = [], s.date()
        while d <= e.date():
            iso = d.isoformat()
            out.append((iso, 1 if iso in have else 0, 1 if iso in half else 0))
            d += timedelta(days=1)
        return out

    # ---- 证券清单 ----
    def list_current_securities(self) -> pd.DataFrame:
        import requests

        def get(url):
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.text

        df = parse_nasdaq_lists(self.th.call(get, NASDAQ_LISTED), self.th.call(get, OTHER_LISTED))
        df["board"] = "us"
        return df

    def security_basic(self, symbol: str) -> dict:
        return {"status": "active", "sec_type": "stock"}

    # ---- 日线 + 拆股 / 分红 ----
    def daily_bars_batch(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """批量日线（一次请求同时取回拆股与分红）。整批为空视为限流（退避重试），个别股票为空视为无数据（权证等）。"""
        end_excl = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        ysyms = [to_yahoo(s) for s in symbols]

        def once():
            df = self.yf.download(ysyms, start=start, end=end_excl, auto_adjust=False, actions=True, progress=False,
                                  group_by="ticker", threads=False)
            if df is None or df.empty:
                raise RateLimited("yfinance 返回空数据：按限流处理")
            return df

        raw = self.th.call(once)
        out: dict[str, pd.DataFrame] = {}
        for s, ys in zip(symbols, ysyms):
            try:
                sub = raw[ys] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = standardize_yf(sub)
            if len(df):
                out[s] = df
        return out

    def daily_bars(self, symbol: str, start: str, end: str, prev_close=None, prev_factor=None) -> pd.DataFrame:
        r = self.daily_bars_batch([symbol], start, end)
        return r.get(symbol, pd.DataFrame(columns=US_BAR_COLS))

    def index_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        end_excl = (date.fromisoformat(end) + timedelta(days=1)).isoformat()

        def once():
            df = self.yf.download(symbol, start=start, end=end_excl, auto_adjust=False, progress=False, threads=False)
            if df is None or df.empty:
                raise RateLimited(f"{symbol} 返回空数据")
            return df

        df = self.th.call(once)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        out = pd.DataFrame({"date": df.index.strftime("%Y-%m-%d"), "open": df["Open"].to_numpy(), "high": df["High"].to_numpy(),
                            "low": df["Low"].to_numpy(), "close": df["Close"].to_numpy(),
                            "volume": df["Volume"].fillna(0).to_numpy()})
        out["amount"] = out["close"] * out["volume"]
        return out.dropna(subset=["close"]).reset_index(drop=True)

    # ---- 行业 / 财报日期（逐股接口：只对候选 / 持仓 / 入库幸存者按需拉取）----
    def industry(self, symbol: str) -> tuple[str | None, str | None]:
        def once():
            info = self.yf.Ticker(to_yahoo(symbol)).info
            if not info:
                raise RateLimited("info 为空")
            return info

        info = self.th.call(once)
        return info.get("industry"), info.get("sector")

    def earnings_dates(self, symbol: str) -> list[tuple[str, bool]]:
        """[(日期, 是否已披露)]。已披露 = Reported EPS 非空；未披露的未来日期多为预估。"""
        def once():
            return self.yf.Ticker(to_yahoo(symbol)).get_earnings_dates(limit=12)

        df = self.th.call(once, retries=1)
        if df is None or df.empty:
            return []
        out = []
        for ts, row in df.iterrows():
            rep = row.get("Reported EPS")
            out.append((pd.Timestamp(ts).date().isoformat(), bool(pd.notna(rep))))
        return out

    # ---- 事件（M6）----
    def news(self, symbol: str, limit: int = 15) -> list[dict]:
        items = self.th.call(lambda: self.yf.Ticker(to_yahoo(symbol)).news, retries=1) or []
        if not items:                       # M0 实测：Ticker.news 在本机常返回空列表，Search 接口仍可用
            items = self.th.call(lambda: self.yf.Search(to_yahoo(symbol), news_count=limit).news, retries=1) or []
        out = []
        for it in items[:limit]:
            c = it.get("content") or it
            title = c.get("title")
            if not title:
                continue
            url = ((c.get("canonicalUrl") or {}).get("url")) or ((c.get("clickThroughUrl") or {}).get("url")) or c.get("link")
            pub = c.get("pubDate") or c.get("displayTime") or ""
            if not pub and it.get("providerPublishTime"):
                pub = pd.Timestamp(it["providerPublishTime"], unit="s", tz="UTC").isoformat()
            out.append({"title": title, "summary": c.get("summary") or "", "url": url, "publish_time": pub,
                        "source": ((c.get("provider") or {}).get("displayName")) or c.get("publisher") or "Yahoo Finance"})
        return out

    def insider_transactions(self, symbol: str) -> list[dict]:
        df = self.th.call(lambda: self.yf.Ticker(to_yahoo(symbol)).insider_transactions, retries=1)
        if df is None or len(df) == 0:
            return []
        out = []
        for r in df.to_dict("records"):
            d = r.get("Start Date")
            out.append({"date": pd.Timestamp(d).date().isoformat() if pd.notna(d) else None, "insider": r.get("Insider"),
                        "position": r.get("Position"), "text": r.get("Text") or r.get("Transaction") or "",
                        "shares": r.get("Shares"), "value": r.get("Value")})
        return out

    def close(self):
        pass


def standardize_yf(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance 单只 DataFrame -> 标准日线。amount = 调整价 × 调整量（价、量同口径调整，等于真实成交额）。"""
    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame(columns=US_BAR_COLS)
    d = df.dropna(subset=["Close"])
    d = d[d["Close"] > 0]
    if d.empty:
        return pd.DataFrame(columns=US_BAR_COLS)
    vol = d["Volume"].fillna(0)
    out = pd.DataFrame({
        "date": pd.to_datetime(d.index).strftime("%Y-%m-%d"),
        "open": d["Open"].to_numpy(), "high": d["High"].to_numpy(), "low": d["Low"].to_numpy(), "close": d["Close"].to_numpy(),
        "volume": vol.to_numpy(), "amount": (d["Close"] * vol).to_numpy(), "turnover": float("nan"),
        "adj_factor": 1.0, "trade_status": 1, "is_st": 0,
        "adj_close": (d["Adj Close"] if "Adj Close" in d.columns else d["Close"]).to_numpy(),
        "dividend": (d["Dividends"] if "Dividends" in d.columns else pd.Series(0.0, index=d.index)).fillna(0).to_numpy(),
        "split": (d["Stock Splits"] if "Stock Splits" in d.columns else pd.Series(0.0, index=d.index)).fillna(0).to_numpy(),
    })
    return out.reset_index(drop=True)[US_BAR_COLS]


def get_us_source():
    if settings.cfg()["datasource"].get("us") == "demo":
        from .datasource_demo_us import DemoUsSource
        return DemoUsSource()
    return UsSource()
