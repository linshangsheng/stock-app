"""美股关键统计（Yahoo Finance 的 Key Statistics 页面内容）：给持仓 / 自选股的详情和「基本面」表用。展示用，不进入选股与回测。

字段：现价、Forward P/E 及其历史、PEG、市净率、52 周区间、50 / 200 日均线、营业利润率、净利率、ROE、季度营收同比、季度盈利同比、
经营现金流、自由现金流、总现金、总负债、流动比率、做空比例、股息率。重点看四样：Forward P/E 及其历史变化、利润率、营收与盈利增速、自由现金流。

来源：yfinance 的 Ticker.info / get_valuation_measures（Key Statistics 页的「Valuation Measures」：当前 + 最近 5 个季度末）/
Yahoo 的 fundamentals-timeseries（更细的 Forward P/E 历史）/ 季度利润表与现金流量表。结果缓存 24 小时（逐股接口，不对全池拉取）。
说明性文字只描述数据（和自己的历史比、和上一季度比），不给买卖建议。
"""
from __future__ import annotations

import json
import logging
import threading
import zlib
from datetime import date, datetime, timedelta

import pandas as pd

from . import db, markets, settings

log = logging.getLogger(__name__)
TTL = timedelta(hours=24)
_f_lock = threading.Lock()
_pending: set[str] = set()
_worker: dict = {"running": False}


def _f(v):
    try:
        x = float(v)
        return None if x != x or x in (float("inf"), float("-inf")) else x
    except (TypeError, ValueError):
        return None


def _iso(x) -> str | None:
    try:
        return pd.to_datetime(x).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


# ---- 取数 -------------------------------------------------------------------------------------------

def _timeseries(t, ysym: str, types: list[str], years: int = 4) -> dict[str, list[tuple[str, float]]]:
    """Yahoo 的 fundamentals-timeseries（Key Statistics 页的估值历史就是它）。失败返回空，不影响其他字段。"""
    out: dict[str, list[tuple[str, float]]] = {}
    try:
        url = f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ysym}"
        p1 = int((datetime.now() - timedelta(days=365 * years)).timestamp())
        r = t._data.cache_get(url, params={"symbol": ysym, "type": ",".join(types), "period1": p1, "period2": int(datetime.now().timestamp()) + 86400,
                                           "merge": "false", "padTimeSeries": "true"})
        for it in r.json()["timeseries"]["result"]:
            k = it["meta"]["type"][0]
            pts = [(x["asOfDate"], _f(x["reportedValue"]["raw"])) for x in (it.get(k) or []) if x and x.get("reportedValue")]
            out[k] = sorted((d, v) for d, v in pts if v is not None)
    except Exception as e:  # noqa: BLE001
        log.info("keystats timeseries %s failed: %s", ysym, e)
    return out


def fetch_us(us, symbol: str) -> dict:
    from .datasource_us import to_yahoo
    ysym = to_yahoo(symbol)
    t = us.yf.Ticker(ysym)
    info = us.th.call(lambda: t.info) or {}
    if not info or (info.get("currentPrice") is None and info.get("regularMarketPrice") is None and info.get("forwardPE") is None):
        raise RuntimeError(f"Yahoo 没有返回 {symbol} 的统计数据（代码不对、ETF / 权证，或被限流）")
    price = _f(info.get("currentPrice")) or _f(info.get("regularMarketPrice")) or _f(info.get("previousClose"))
    rate = _f(info.get("dividendRate"))
    dy = (rate / price) if (rate and price) else None
    if dy is None and _f(info.get("dividendYield")) is not None:
        dy = _f(info.get("dividendYield")) / 100.0                       # 新版 Yahoo 的 dividendYield 已是「百分数」（0.32 = 0.32%）
    stats = {
        "price": price, "forward_pe": _f(info.get("forwardPE")), "trailing_pe": _f(info.get("trailingPE")),
        "peg": _f(info.get("trailingPegRatio")) or _f(info.get("pegRatio")), "price_to_book": _f(info.get("priceToBook")),
        "low_52w": _f(info.get("fiftyTwoWeekLow")), "high_52w": _f(info.get("fiftyTwoWeekHigh")),
        "ma50": _f(info.get("fiftyDayAverage")), "ma200": _f(info.get("twoHundredDayAverage")),
        "operating_margin": _f(info.get("operatingMargins")), "profit_margin": _f(info.get("profitMargins")), "roe": _f(info.get("returnOnEquity")),
        "revenue_growth_q": _f(info.get("revenueGrowth")), "earnings_growth_q": _f(info.get("earningsQuarterlyGrowth")),
        "operating_cashflow": _f(info.get("operatingCashflow")), "free_cashflow": _f(info.get("freeCashflow")),
        "total_cash": _f(info.get("totalCash")), "total_debt": _f(info.get("totalDebt")), "current_ratio": _f(info.get("currentRatio")),
        "short_pct_float": _f(info.get("shortPercentOfFloat")), "dividend_yield": dy,
        "market_cap": _f(info.get("marketCap")), "revenue_ttm": _f(info.get("totalRevenue")), "net_income_ttm": _f(info.get("netIncomeToCommon")),
        "forward_eps": _f(info.get("forwardEps")), "trailing_eps": _f(info.get("trailingEps")), "beta": _f(info.get("beta")),
    }
    # 估值历史：Key Statistics 页的「Valuation Measures」（当前 + 最近 5 个季度末）
    vm_rows = []
    try:
        vm = us.th.call(lambda: t.get_valuation_measures(), retries=1)
        want = {"Trailing P/E": "trailing_pe", "Forward P/E": "forward_pe", "PEG Ratio (5yr expected)": "peg", "Price/Book": "price_to_book", "Price/Sales": "price_to_sales"}
        for col in vm.columns:
            row = {"period": "current" if str(col).lower() == "current" else _iso(col)}
            for lab, key in want.items():
                if lab in vm.index:
                    row[key] = _f(vm.loc[lab, col])
            vm_rows.append(row)
    except Exception as e:  # noqa: BLE001
        log.info("keystats valuation measures %s failed: %s", symbol, e)
    ts = _timeseries(t, ysym, ["trailingForwardPeRatio", "trailingPegRatio"])
    fwd_hist = list(ts.get("trailingForwardPeRatio") or [])
    for r in vm_rows:                                    # 再并入 Key Statistics 页的季度末数据（与细序列相差 20 天以内的点不重复加）
        if r.get("period") in (None, "current") or not r.get("forward_pe"):
            continue
        if all(abs((date.fromisoformat(r["period"]) - date.fromisoformat(d)).days) > 20 for d, _ in fwd_hist):
            fwd_hist.append((r["period"], r["forward_pe"]))
    fwd_hist.sort()
    # 季度利润表 / 现金流量表（最近约 5 个季度）
    quarters = []
    try:
        qi = us.th.call(lambda: t.quarterly_income_stmt, retries=1)
        qc = None
        try:
            qc = us.th.call(lambda: t.quarterly_cashflow, retries=1)
        except Exception:  # noqa: BLE001
            qc = None
        for col in list(qi.columns)[:6]:
            def g(df, name):
                return _f(df.loc[name, col]) if df is not None and name in df.index and col in df.columns else None
            rev = g(qi, "Total Revenue")
            oi = g(qi, "Operating Income")
            ni = g(qi, "Net Income")
            quarters.append({"date": _iso(col), "revenue": rev, "operating_income": oi, "net_income": ni,
                             "operating_margin": (oi / rev) if (oi is not None and rev) else None, "net_margin": (ni / rev) if (ni is not None and rev) else None,
                             "free_cashflow": g(qc, "Free Cash Flow"), "operating_cashflow": g(qc, "Operating Cash Flow")})
        quarters.sort(key=lambda q: q["date"] or "")
    except Exception as e:  # noqa: BLE001
        log.info("keystats quarterly %s failed: %s", symbol, e)
    return {"symbol": symbol, "name": info.get("shortName") or info.get("longName"), "sector": info.get("sector"), "industry": info.get("industry"),
            "currency": info.get("currency") or "USD", "stats": stats, "valuation_table": vm_rows,
            "fwd_pe_history": [{"date": d, "value": v} for d, v in fwd_hist], "quarters": quarters}


def demo_us(symbol: str) -> dict:
    """合成演示数据（离线测试 / 演示模式）：由代码哈希确定，每次相同。"""
    h = zlib.crc32(symbol.encode())
    price = 20 + h % 300
    today = date.today()
    base_pe = 14 + h % 20
    hist = [{"date": (today - timedelta(days=30 * k)).isoformat(), "value": round(base_pe * (1 - 0.012 * k + 0.02 * ((h >> k) % 3)), 2)} for k in range(24, -1, -2)]
    rev0 = 1e9 * (1 + h % 40)
    quarters = []
    for k in range(5):
        end = today - timedelta(days=90 * (4 - k))
        rev = rev0 * (1 + 0.03 * k)
        quarters.append({"date": end.isoformat(), "revenue": rev, "operating_income": rev * (0.18 + 0.004 * k), "net_income": rev * (0.12 + 0.003 * k),
                         "operating_margin": 0.18 + 0.004 * k, "net_margin": 0.12 + 0.003 * k, "free_cashflow": rev * (0.08 + 0.004 * k), "operating_cashflow": rev * 0.14})
    stats = {"price": float(price), "forward_pe": float(base_pe), "trailing_pe": base_pe * 1.15, "peg": 1.0 + (h % 20) / 10, "price_to_book": 2 + (h % 50) / 10,
             "low_52w": price * 0.7, "high_52w": price * 1.15, "ma50": price * 0.97, "ma200": price * 0.92, "operating_margin": 0.2, "profit_margin": 0.14, "roe": 0.17,
             "revenue_growth_q": 0.09, "earnings_growth_q": 0.14, "operating_cashflow": rev0 * 0.5, "free_cashflow": rev0 * 0.32, "total_cash": rev0 * 0.8, "total_debt": rev0 * 0.6,
             "current_ratio": 1.6, "short_pct_float": 0.03, "dividend_yield": 0.012, "market_cap": price * 1e8, "revenue_ttm": rev0 * 4.5, "net_income_ttm": rev0 * 0.6,
             "forward_eps": price / base_pe, "trailing_eps": price / base_pe / 1.15, "beta": 1.1}
    vm = [{"period": "current", "forward_pe": float(base_pe), "trailing_pe": base_pe * 1.15, "peg": stats["peg"], "price_to_book": stats["price_to_book"]}] + \
         [{"period": (today - timedelta(days=90 * k)).isoformat(), "forward_pe": base_pe * (1 - 0.02 * k), "trailing_pe": base_pe * 1.1, "peg": 1.2, "price_to_book": 2.5} for k in range(1, 6)]
    return {"symbol": symbol, "name": f"演示 {symbol}", "sector": "演示", "industry": "演示", "currency": "USD", "stats": stats, "valuation_table": vm,
            "fwd_pe_history": hist, "quarters": quarters}


# ---- 解读（只描述数据，不给建议） -----------------------------------------------------------------------

def _near(hist: list[dict], days: int, tol: int = 50) -> dict | None:
    """历史里离「今天往前 days 天」最近的一点（超过 tol 天没有数据就不用）。"""
    if not hist:
        return None
    target = date.today() - timedelta(days=days)
    best = min(hist, key=lambda p: abs((date.fromisoformat(p["date"]) - target).days))
    return best if abs((date.fromisoformat(best["date"]) - target).days) <= tol else None


def _price_change_since(conn, symbol: str, d0: str) -> float | None:
    try:
        r0 = conn.execute("SELECT close, adj_factor FROM daily_bar WHERE symbol=? AND date>=? AND trade_status>0 ORDER BY date LIMIT 1", (symbol, d0)).fetchone()
        r1 = conn.execute("SELECT close, adj_factor FROM daily_bar WHERE symbol=? AND trade_status>0 ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
        if not r0 or not r1 or not r0[0]:
            return None
        return (r1[0] * r1[1]) / (r0[0] * r0[1]) - 1                # 用复权价，排除拆股的影响
    except Exception:  # noqa: BLE001
        return None


def _pp(x):
    return f"{x * 100:+.1f} 个百分点"


def _p(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def derive(d: dict, conn=None) -> dict:
    s, hist, qs = d["stats"], d.get("fwd_pe_history") or [], d.get("quarters") or []
    flags: list[str] = []
    out: dict = {}
    # ① Forward P/E 及其历史变化
    fpe = s.get("forward_pe")
    v = {"title": "Forward P/E 及其历史变化", "level": "na", "headline": "没有 Forward P/E（公司没有分析师盈利预期，或预期为亏损）", "bullets": [], "changes": {}}
    if fpe:
        v["headline"] = f"Forward P/E {fpe:.1f}"
        tpe = s.get("trailing_pe")
        if tpe:
            v["bullets"].append(f"Forward P/E {fpe:.1f} {'低于' if fpe < tpe else '高于'}过去 12 个月市盈率 {tpe:.1f}："
                                + ("分析师预期未来盈利比过去 12 个月更高。" if fpe < tpe else "分析师预期未来盈利比过去 12 个月更低（或不变）。"))
        for lab, days in (("3 个月前", 91), ("半年前", 182), ("1 年前", 365)):
            p = _near(hist, days)
            if p and p["value"]:
                ch = fpe / p["value"] - 1
                v["changes"][lab] = {"date": p["date"], "value": p["value"], "chg": round(ch, 4)}
        if v["changes"]:
            lab = "1 年前" if "1 年前" in v["changes"] else list(v["changes"])[-1]
            c = v["changes"][lab]
            v["headline"] += f"，比 {lab}（{c['value']:.1f}）{'高' if c['chg'] > 0 else '低'} {abs(c['chg']) * 100:.0f}%"
            if conn is not None:
                pc = _price_change_since(conn, d["symbol"], c["date"])
                if pc is not None:
                    eps_chg = (1 + pc) / (1 + c["chg"]) - 1
                    v["bullets"].append(f"同期股价 {pc * 100:+.0f}%，所以隐含的未来每股盈利预期约 {eps_chg * 100:+.0f}%："
                                        + ("估值抬升主要来自股价上涨，盈利预期变化不大。" if abs(eps_chg) < abs(pc) / 2 and c["chg"] > 0.05
                                           else "盈利预期也在同方向变化。" if (eps_chg > 0) == (pc > 0) else "股价和盈利预期方向相反。"))
        vals = [p["value"] for p in hist if p.get("value")]
        if len(vals) >= 6:
            pct = sum(1 for x in vals if x <= fpe) / len(vals)
            v["bullets"].append(f"在它自己近 {len(vals)} 个记录点（约 {max(1, round((date.today() - date.fromisoformat(hist[0]['date'])).days / 365))} 年）里处于 {pct * 100:.0f}% 分位（区间 {min(vals):.1f} ~ {max(vals):.1f}）。")
            if pct >= 0.8:
                flags.append("Forward P/E 处在自己历史的高位")
            elif pct <= 0.2:
                v["bullets"].append("处在自己历史的低位区。")
            v["pct_in_history"] = round(pct, 3)
        peg = s.get("peg")
        if peg:
            v["bullets"].append(f"PEG {peg:.2f}：市盈率相对预期盈利增速{'偏高' if peg > 2 else '偏低' if peg < 1 else '适中'}（常见参考线 1~2；增速预期来自分析师，波动大）。")
            if peg > 2.5:
                flags.append(f"PEG {peg:.1f} 偏高")
        v["level"] = "warn" if v.get("pct_in_history", 0) >= 0.8 or (s.get("peg") or 0) > 2.5 else "neutral"
    out["valuation"] = v
    # ② 利润率
    om, nm = s.get("operating_margin"), s.get("profit_margin")
    m = {"title": "利润率", "level": "na", "headline": "没有利润率数据", "bullets": []}
    if om is not None or nm is not None:
        m["headline"] = f"营业利润率 {_p(om)} · 净利率 {_p(nm)}" + (f" · ROE {_p(s.get('roe'), 0)}" if s.get("roe") is not None else "")
        qm = [q for q in qs if q.get("operating_margin") is not None]
        if len(qm) >= 3:
            last, prev = qm[-1], qm[:-1]
            avg = sum(q["operating_margin"] for q in prev) / len(prev)
            dlt = last["operating_margin"] - avg
            m["bullets"].append(f"最近一季营业利润率 {_p(last['operating_margin'])}，比前 {len(prev)} 个季度平均（{_p(avg)}）{_pp(dlt)}："
                                + ("利润率在扩张。" if dlt > 0.01 else "利润率在收缩。" if dlt < -0.01 else "基本稳定。"))
            m["trend"] = round(dlt, 4)
            m["level"] = "good" if dlt > 0.01 else "warn" if dlt < -0.01 else "neutral"
            if dlt < -0.02:
                flags.append("营业利润率明显收缩")
        else:
            m["level"] = "neutral"
        if (om is not None and om < 0) or (nm is not None and nm < 0):
            m["level"] = "warn"
            flags.append("公司目前亏损（利润率为负）")
    out["margins"] = m
    # ③ 营收与盈利增速
    rg, eg = s.get("revenue_growth_q"), s.get("earnings_growth_q")
    g = {"title": "营收与盈利增速（最近一季，同比）", "level": "na", "headline": "没有增速数据", "bullets": []}
    if rg is not None or eg is not None:
        g["headline"] = f"营收同比 {_p(rg)} · 盈利同比 {_p(eg)}"

        def band(x):
            return "高增长" if x > 0.2 else "稳健增长" if x > 0.05 else "低增长" if x >= 0 else "同比下降"
        if rg is not None:
            g["bullets"].append(f"营收：{band(rg)}（{_p(rg)}）。")
        if eg is not None:
            g["bullets"].append(f"盈利：{band(eg)}（{_p(eg)}）。")
        if rg is not None and eg is not None:
            if eg > rg + 0.05:
                g["bullets"].append("盈利增速快于营收增速：多半是利润率在提升。")
            elif eg < rg - 0.05:
                g["bullets"].append("盈利增速慢于营收增速：成本或费用增长更快，利润率承压。")
        revq = [q for q in qs if q.get("revenue")]
        if len(revq) >= 5:
            prior = revq[-2]["revenue"] / revq[-6]["revenue"] - 1 if len(revq) >= 6 else None
            now = revq[-1]["revenue"] / revq[-5]["revenue"] - 1
            if prior is not None:
                g["bullets"].append(f"上一季营收同比 {_p(prior)} → 最近一季 {_p(now)}：增速{'在加快' if now > prior + 0.02 else '在放缓' if now < prior - 0.02 else '大致持平'}。")
        g["level"] = "warn" if (rg is not None and rg < 0) or (eg is not None and eg < 0) else "good" if (rg or 0) > 0.1 and (eg or 0) > 0.1 else "neutral"
        if rg is not None and rg < 0:
            flags.append("营收同比下降")
        if eg is not None and eg < -0.2:
            flags.append("盈利同比大幅下降")
    out["growth"] = g
    # ④ 自由现金流
    fcf, ocf, rev, mc, ni = s.get("free_cashflow"), s.get("operating_cashflow"), s.get("revenue_ttm"), s.get("market_cap"), s.get("net_income_ttm")
    c = {"title": "自由现金流", "level": "na", "headline": "没有现金流数据", "bullets": []}
    if fcf is not None:
        c["headline"] = f"自由现金流（近 12 个月）{'为正' if fcf > 0 else '为负'}"
        if rev:
            c["fcf_margin"] = round(fcf / rev, 4)
            c["bullets"].append(f"自由现金流占营收 {_p(fcf / rev)}（每 100 美元营收留下 {fcf / rev * 100:.1f} 美元现金）。")
        if mc and fcf:
            c["fcf_yield"] = round(fcf / mc, 4)
            c["bullets"].append(f"自由现金流 ÷ 市值 = {_p(fcf / mc)}（现金流回报率，越高说明同样的价格买到的现金越多）。")
        if ni and ni > 0 and fcf is not None:
            r = fcf / ni
            c["bullets"].append(f"自由现金流是净利润的 {r:.1f} 倍：" + ("利润基本变成了真金白银。" if r >= 0.9 else "利润里有一部分没有变成现金（应收、存货、资本开支占用）。" if r >= 0.5 else "利润大部分没有变成现金，留意。"))
            c["fcf_to_ni"] = round(r, 2)
            if r < 0.5:
                flags.append("自由现金流远低于净利润")
        fq = [q for q in qs if q.get("free_cashflow") is not None]
        if len(fq) >= 3:
            c["bullets"].append("最近几个季度自由现金流：" + " → ".join(f"{q['free_cashflow'] / 1e8:.1f} 亿" for q in fq[-4:]) + "（美元）。")
        c["level"] = "warn" if fcf < 0 else "good" if (rev and fcf / rev > 0.1) else "neutral"
        if fcf < 0:
            flags.append("自由现金流为负")
    out["cashflow"] = c
    # 其他：资产负债、价格位置、做空
    tc, td, cr = s.get("total_cash"), s.get("total_debt"), s.get("current_ratio")
    if tc is not None and td is not None:
        out["balance"] = {"net_cash": tc - td, "text": f"总现金 {tc / 1e8:.1f} 亿、总负债 {td / 1e8:.1f} 亿：" + ("净现金" if tc >= td else "净负债") + f" {abs(tc - td) / 1e8:.1f} 亿（美元）"
                          + (f"；流动比率 {cr:.2f}" + ("（短期偿债缓冲薄）" if cr < 1 else "") if cr is not None else "")}
        if cr is not None and cr < 1:
            flags.append(f"流动比率 {cr:.2f} < 1")
    px, lo, hi, m50, m200 = s.get("price"), s.get("low_52w"), s.get("high_52w"), s.get("ma50"), s.get("ma200")
    if px and lo and hi and hi > lo:
        out["range_pos"] = round((px - lo) / (hi - lo), 3)
    if px and m200:
        out["vs_ma200"] = round(px / m200 - 1, 4)
        if px < m200:
            flags.append("股价在 200 日均线下方")
    if px and m50:
        out["vs_ma50"] = round(px / m50 - 1, 4)
    sp = s.get("short_pct_float")
    if sp is not None and sp >= 0.1:
        flags.append(f"做空比例 {sp * 100:.0f}% 偏高")
    out["flags"] = flags
    return out


def row_summary(d: dict) -> dict:
    """列表（「基本面」表）用的精简字段。"""
    s, a = d["stats"], d.get("analysis") or {}
    ch = (a.get("valuation") or {}).get("changes") or {}
    c1 = ch.get("1 年前") or (list(ch.values())[-1] if ch else None)
    return {"symbol": d["symbol"], "name": d.get("name"), "price": s.get("price"), "forward_pe": s.get("forward_pe"), "fwd_pe_chg": c1 and c1["chg"],
            "fwd_pe_chg_label": next((k for k, v in ch.items() if c1 is v), None), "pe_pct": (a.get("valuation") or {}).get("pct_in_history"),
            "operating_margin": s.get("operating_margin"), "profit_margin": s.get("profit_margin"), "margin_trend": (a.get("margins") or {}).get("trend"),
            "revenue_growth_q": s.get("revenue_growth_q"), "earnings_growth_q": s.get("earnings_growth_q"),
            "free_cashflow": s.get("free_cashflow"), "fcf_yield": (a.get("cashflow") or {}).get("fcf_yield"), "range_pos": a.get("range_pos"),
            "vs_ma200": a.get("vs_ma200"), "flags": a.get("flags") or [], "fetched_at": d.get("fetched_at")}


# ---- 缓存与入口 --------------------------------------------------------------------------------------

def _read(conn, symbol: str) -> dict | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (f"ks:{symbol}",)).fetchone()
    try:
        return json.loads(r[0]) if r else None
    except ValueError:
        return None


def _fresh(d: dict | None) -> bool:
    try:
        return bool(d) and datetime.now() - datetime.fromisoformat(d["fetched_at"]) < TTL
    except (KeyError, ValueError):
        return False


def get_one(conn, symbol: str, src, force: bool = False) -> dict:
    d = _read(conn, symbol)
    if _fresh(d) and not force:
        return d
    try:
        d = demo_us(symbol) if markets.is_demo("US") else fetch_us(src, symbol)
    except Exception:
        if d:                                              # 上游失败：用旧缓存并标注
            d["stale"] = True
            return d
        raise
    d["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    d["analysis"] = derive(d, conn)
    db.set_meta(conn, f"ks:{symbol}", json.dumps(d, ensure_ascii=False))
    return d


def _work():
    from .datasource_cn import get_source
    while True:
        with _f_lock:
            if not _pending:
                _worker["running"] = False
                return
            sym = sorted(_pending)[0]
            _pending.discard(sym)
        src = None
        try:
            with settings.market_ctx("US"):
                src = get_source("US")
                with db.market_db("US") as c:
                    get_one(c, sym, src)
        except Exception as e:  # noqa: BLE001
            log.info("keystats background %s failed: %s", sym, e)
            _failed[sym] = str(e)[:120]
        finally:
            if src is not None and hasattr(src, "close"):
                src.close()


_failed: dict[str, str] = {}


def get_many(conn, symbols: list[str]) -> dict:
    """列表用：缓存里有的立即返回（过期的也先给旧的），缺的和过期的在后台逐只补齐（串行，受限速保护）。"""
    rows, need = {}, []
    for s in symbols:
        d = _read(conn, s)
        if d and d.get("analysis"):
            rows[s] = {**row_summary(d), "stale": not _fresh(d)}
        if not _fresh(d):
            need.append(s)
    pending = [s for s in need if s not in _failed]
    if pending:
        with _f_lock:
            _pending.update(pending)
            if not _worker["running"]:
                _worker["running"] = True
                threading.Thread(target=_work, name="keystats", daemon=True).start()
    with _f_lock:
        busy = [s for s in symbols if s in _pending]
    return {"rows": rows, "pending": busy, "failed": {s: _failed[s] for s in symbols if s in _failed and s not in rows}}
