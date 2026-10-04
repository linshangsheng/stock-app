"""公司基本面摘要（3.15，M6 /api/financials）。展示用，**不进入回测特征**（除非点时可用）：
  * A 股：BaoStock 季频财务，**带公告日 pubDate**（点时可用，回测中只能在公告日之后使用）；
  * 美股：yfinance 的财务与基本面**不含披露时间**，第一阶段只用于展示与入库粗筛；如需回测须改用带 filing 时间的 SEC XBRL。
结果缓存 7 天（逐股接口，不对全池拉取）。"""
from __future__ import annotations

import json
import zlib
from datetime import date, datetime, timedelta

from . import db, throttle


def _quarters(n: int, today: date | None = None) -> list[tuple[int, int]]:
    """最近 n 个已结束的季度 (year, quarter)。"""
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3          # 当前季度尚未结束：从上一季度开始
    out = []
    while len(out) < n:
        if q == 0:
            y, q = y - 1, 4
        out.append((y, q))
        q -= 1
    return out


def _f(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def cn_financials(bs, symbol: str, n: int = 6) -> dict:
    rows = []
    for y, q in _quarters(n):
        try:
            p = bs._query("query_profit_data", code=symbol, year=y, quarter=q)
        except throttle.CircuitOpen:
            raise
        except Exception:  # noqa: BLE001
            continue
        if p.empty:
            continue
        r = p.iloc[0]
        row = {"stat_date": r["statDate"], "pub_date": r["pubDate"], "roe": _f(r["roeAvg"]), "net_margin": _f(r["npMargin"]),
               "gross_margin": _f(r["gpMargin"]), "net_profit": _f(r["netProfit"]), "eps_ttm": _f(r["epsTTM"]), "revenue": _f(r["MBRevenue"])}
        try:
            b = bs._query("query_balance_data", code=symbol, year=y, quarter=q)
            if not b.empty:
                row["debt_ratio"] = _f(b.iloc[0]["liabilityToAsset"])
            g = bs._query("query_growth_data", code=symbol, year=y, quarter=q)
            if not g.empty:
                row["yoy_net_profit"] = _f(g.iloc[0]["YOYNI"])
                row["yoy_eps"] = _f(g.iloc[0]["YOYEPSBasic"])
        except throttle.CircuitOpen:
            raise
        except Exception:  # noqa: BLE001
            pass
        rows.append(row)
    return {"symbol": symbol, "currency": "CNY", "pit": True, "periods": rows,
            "note": "BaoStock 季频财务，带公告日（pub_date）：回测只能在公告日之后使用（点时，3.15）。金额单位：元。"}


def us_financials(us, symbol: str) -> dict:
    yf = us.yf
    from .datasource_us import to_yahoo
    t = yf.Ticker(to_yahoo(symbol))
    info = us.th.call(lambda: t.info) or {}
    keys = ["marketCap", "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months", "trailingEps", "dividendYield",
            "returnOnEquity", "grossMargins", "operatingMargins", "profitMargins", "debtToEquity", "freeCashflow", "revenueGrowth",
            "earningsGrowth", "sharesShort", "shortPercentOfFloat", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"]
    snap = {k: _f(info.get(k)) for k in keys}
    periods = []
    try:
        q = us.th.call(lambda: t.quarterly_income_stmt, retries=1)
        for col in list(q.columns)[:6]:
            def g(name):
                return _f(q.loc[name, col]) if name in q.index else None
            periods.append({"stat_date": col.date().isoformat(), "pub_date": None, "revenue": g("Total Revenue"), "net_profit": g("Net Income"),
                            "eps_basic": g("Basic EPS"), "gross_profit": g("Gross Profit")})
    except Exception:  # noqa: BLE001
        pass
    return {"symbol": symbol, "currency": "USD", "pit": False, "snapshot": snap, "sector": info.get("sector"), "industry": info.get("industry"),
            "periods": periods, "note": "yfinance 的财务 / 基本面不含披露时间：仅用于展示与入库粗筛，不进入回测特征（3.15）。"}


def demo_financials(symbol: str, market: str) -> dict:
    h = zlib.crc32(symbol.encode())
    base = 1e9 * (1 + h % 50)
    periods = []
    for i, (y, q) in enumerate(_quarters(6, date.today())):
        end = date(y, q * 3, [31, 30, 30, 31][q - 1])
        pub = (end + timedelta(days=30 + h % 10)).isoformat() if market == "CN" else None
        rev = base * (1 + 0.04 * (6 - i))
        periods.append({"stat_date": end.isoformat(), "pub_date": pub, "revenue": rev, "net_profit": rev * 0.1, "roe": 0.02 + 0.001 * (h % 10),
                        "net_margin": 0.1, "gross_margin": 0.3, "eps_ttm": 1.0 + 0.1 * i})
    out = {"symbol": symbol, "currency": "CNY" if market == "CN" else "USD", "pit": market == "CN", "periods": periods, "note": "合成演示数据。"}
    if market == "US":
        out["snapshot"] = {"marketCap": base * 20, "trailingPE": 18.5, "priceToBook": 3.2, "shortPercentOfFloat": 0.03}
    return out


def get_financials(conn, market: str, symbol: str, src, force: bool = False) -> dict:
    """7 天缓存。"""
    from . import markets
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"fin:{symbol}",)).fetchone()
    if row and not force:
        d = json.loads(row[0])
        try:
            if datetime.now() - datetime.fromisoformat(d["fetched_at"]) < timedelta(days=7):
                return d
        except (KeyError, ValueError):
            pass
    if markets.is_demo(market):
        d = demo_financials(symbol, market)
    elif market == "CN":
        d = cn_financials(src, symbol)
    else:
        d = us_financials(src, symbol)
    d["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    db.set_meta(conn, f"fin:{symbol}", json.dumps(d, ensure_ascii=False))
    return d
